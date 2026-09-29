"""Independent Phase 3 evidence and reporting; no strategy changes."""
import ast
import csv
import hashlib
import json
import sqlite3
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import config
from .models import timestamp, parse_time
from .storage import Storage, StorageError, atomic_write
from .portfolio import validate_state
from .execution import DepthFill, liquidation
from .performance import metrics, drawdown
from .strategy.strategy_engine import StrategyEngine
from .strategy.risk_manager import completed_positions

D = Decimal
SIGNAL_FIELDS = ['timestamp', 'market_id', 'market_title', 'strategy', 'signal_score',
                 'combined_score', 'bid', 'ask', 'spread', 'volume', 'liquidity', 'decision',
                 'decision_reason', 'outcome', 'event_id', 'decision_id']


def write_json(path, data):
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2, default=str) + '\n')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sources(root):
    # Legacy Phase 3 is a frozen GET-only experiment. The later zero-cost reproduction
    # is a separate experiment with its own audit and includes a Gemini POST client, so
    # it must not silently widen the scope of this historical read-only audit.
    free_modules = {'broad_scanner.py', 'free_config.py', 'gemini_free.py', 'free_strategy.py', 'free_runtime.py', 'free_evidence.py'}
    legacy_src = [p for p in sorted((root / 'src').rglob('*.py')) if p.name not in free_modules]
    return [root / 'config.py', root / 'main.py', root / 'phase3.py',
            *([root/'vps.py'] if (root/'vps.py').exists() else []), *legacy_src]


def source_hashes(root):
    return {p.relative_to(root).as_posix(): sha(p) for p in sources(root)}


def settings():
    return {k: str(v) if isinstance(v, D) else v for k, v in asdict(config.StrategyConfig()).items()}


def snapshot(root):
    return dict(strategy=settings(), runtime={k: str(v) if isinstance(v, D) else v
        for k, v in vars(config).items() if k.isupper() and k != 'ROOT'}, duration_seconds=172800,
        heartbeat_seconds=300, summary_seconds=3600, mode='PAPER', source_hashes=source_hashes(root),
        fee_model='Phase2 FeeInfo.calculate per actual depth leg',
        fill_model='Phase2 DepthFill BUY asks SELL bids; actual VWAP; no synthetic spread',
        valuation='full executable bid depth before hypothetical exit fees; missing/stale = null',
        unsupported_features=['official paper settlement: not implemented; ended/unavailable positions retained UNRESOLVED'],
        clock_source='HTTPS Date on public API responses; 30 second tolerance',
        clock_documentation='https://docs.polymarket.com/api-reference/data/get-server-time')


def static_audit(root):
    violations, request_sites = [], []
    forbidden = {'web3', 'eth_account', 'py_clob_client', 'py_clob_client_v2', 'ccxt'}
    prohibited = {'post', 'put', 'delete', 'patch', 'sign_transaction', 'send_raw_transaction',
                  'create_order', 'post_order', 'sign_message', 'redeem_positions'}
    for path in sources(root):
        relative = path.relative_to(root).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [x.name for x in node.names] if isinstance(node, ast.Import) else [node.module or '']
                if any(n.split('.')[0] in forbidden for n in names):
                    violations.append(relative + ': prohibited import')
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, 'attr', '')
                if name in prohibited:
                    violations.append(relative + ': prohibited call ' + name)
                if name == 'Request':
                    methods = [k.value.value for k in node.keywords if k.arg == 'method' and isinstance(k.value, ast.Constant)]
                    request_sites.append(dict(file=relative, line=node.lineno, methods=methods))
                    if methods != ['GET'] or relative != 'src/polymarket_client.py':
                        violations.append(relative + ': unexpected network request')
    return dict(result='PASS' if not violations else 'FAIL', violations=violations, request_sites=request_sites,
                scope='AST audit plus frozen allowlisted public GET client; not a general security proof')


def check_frozen(directory, root):
    manifest = read_json(directory / 'manifest.json')
    snap = read_json(directory / 'config_snapshot.json')
    if sha(directory / 'config_snapshot.json') != manifest['config_hash']:
        raise StorageError('INVALID: configuration hash changed')
    if source_hashes(root) != snap['source_hashes'] or settings() != snap['strategy']:
        raise StorageError('INVALID: frozen code/parameters changed')
    origin = directory / 'time_origin.json'
    if origin.exists() and manifest.get('start_time') is not None and read_json(origin) != manifest:
        raise StorageError('INVALID: experiment identity/deadline changed')
    config.validate_trading_mode()
    return True


class EvidenceStorage(Storage):
    """SQLite is an append-only signal/trade witness; JSON remains the balance authority.

    CSVs are views, regenerated on restart from unique witness records. A state commit
    interrupted before its witness commit is reconciled only by appending its suffix.
    A shortened or changed witnessed ledger is refused, never silently repaired.
    """
    def __enter__(self):
        super().__enter__()
        self.db = sqlite3.connect(self.directory / 'evidence.sqlite3')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS signals (id TEXT PRIMARY KEY, rejected INTEGER, body TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, body TEXT)')
        for table in ('signals', 'trades'):
            for operation in ('UPDATE', 'DELETE'):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'append-only evidence'); END")
        self.db.commit()
        return self

    def __exit__(self, *args):
        if hasattr(self, 'db'):
            self.db.close()
        super().__exit__(*args)

    def witness(self, state, append=True):
        rows = list(self.db.execute('SELECT id, body FROM trades ORDER BY id'))
        trades = state['trades']
        if len(rows) > len(trades):
            raise StorageError('INVALID: witnessed trades deleted')
        for index, (_, body) in enumerate(rows):
            if json.loads(body) != trades[index]:
                raise StorageError('INVALID: witnessed trade altered')
        if append:
            with self.db:
                for t in trades[len(rows):]:
                    self.db.execute('INSERT INTO trades VALUES (?, ?)', (t['trade_id'], json.dumps(t, sort_keys=True)))
        return True

    def load(self, balance):
        state = super().load(balance)
        self.witness(state)
        self.rebuild_signals()
        for trade in state['trades']:
            self.record_fill(trade)
        return state

    def record_fill(self, trade):
        self.signal(dict(timestamp=trade['timestamp'], market_id=trade['market_id'], market_title=trade['market_title'],
            strategy=trade.get('strategy') or 'phase1/manual', signal_score=trade.get('signal_score'),
            combined_score=trade.get('combined_score'), bid=trade.get('entry_bid') if trade['side']=='BUY' else trade.get('exit_bid'),
            ask=trade.get('entry_ask') if trade['side']=='BUY' else trade.get('exit_ask'), spread=trade['spread'],
            volume=trade.get('volume'), liquidity=trade.get('liquidity'), decision=trade['side']+'_FILLED',
            decision_reason=trade['reason'], outcome=trade['outcome'], event_id='trade_'+str(trade['trade_id']),
            decision_id=trade.get('decision_id')), False)

    def save(self, state):
        self.witness(state, append=False)
        super().save(state)
        self.witness(state)

    def signal(self, row, rejected):
        body = json.dumps(row, default=str, ensure_ascii=False)
        with self.db:
            cursor = self.db.execute('INSERT OR IGNORE INTO signals VALUES (?, ?, ?)', (row['event_id'], int(rejected), body))
        if cursor.rowcount:
            self.append_csv('signals.csv', row)
            if rejected:
                self.append_csv('rejected_signals.csv', row)

    def rebuild_signals(self):
        # Atomic streaming export, bounded memory for a 48-hour experiment.
        import os
        import tempfile
        for name, query in [('signals.csv', 'SELECT body FROM signals ORDER BY rowid'),
                            ('rejected_signals.csv', 'SELECT body FROM signals WHERE rejected=1 ORDER BY rowid')]:
            fd, temporary = tempfile.mkstemp(dir=self.directory, prefix=name, suffix='.tmp')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
                    writer = csv.DictWriter(stream, SIGNAL_FIELDS)
                    writer.writeheader()
                    for (body,) in self.db.execute(query):
                        writer.writerow(json.loads(body))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.directory / name)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)


class RecordedEngine(StrategyEngine):
    def __init__(self, *args, **kwargs):
        stored = args[0].state.get('strategy_state')
        if stored and stored['config'] != settings():
            raise StorageError('INVALID: persisted strategy parameters differ')
        super().__init__(*args, **kwargs)
        # Audit sequence can be ahead of the latest state if a process crashed.
        latest = self.storage.db.execute("SELECT MAX(CAST(json_extract(body, '$.decision_id') AS INTEGER)) FROM signals").fetchone()[0]
        self.state['decision_sequence'] = max(self.state['decision_sequence'], latest or 0)

    def audit(self, now, market, outcome, action, reasons, signals=None, score=None, risk=None, features=None):
        identity = super().audit(now, market, outcome, action, reasons, signals, score, risk, features)
        quote = market.quotes.get(outcome)
        for sig in signals or [None]:
            strategy = sig.strategy if sig else 'exit_manager'
            event_id = hashlib.sha256('|'.join([now.isoformat(), market.market_id, outcome, action, strategy]).encode()).hexdigest()
            row = dict(timestamp=now.isoformat(), market_id=market.market_id, market_title=market.title,
                strategy=strategy, signal_score=str(sig.score) if sig else None,
                combined_score=str(score) if score is not None else None,
                bid=str(quote.bid) if quote and quote.bid is not None else None,
                ask=str(quote.ask) if quote and quote.ask is not None else None,
                spread=str(quote.spread) if quote and quote.spread is not None else None,
                volume=str(market.volume) if market.volume is not None else None,
                liquidity=str(market.liquidity) if market.liquidity is not None else None,
                decision=action, decision_reason=';'.join(list(reasons) + ([sig.reason] if sig else [])),
                outcome=outcome, event_id=event_id, decision_id=identity)
            self.storage.signal(row, action not in ('entry_candidate', 'exit_candidate'))
        return identity


def mark_bid(state, markets):
    equity, unrealized, missing = D(state['cash_balance']), D(0), []
    for key, p in state['positions'].items():
        try:
            quote = markets[p['market_id']].quotes[p['outcome']]
            quote.require_fresh(config.MAX_QUOTE_AGE_SECONDS)
            fill = DepthFill().execute('SELL', D(p['quantity']), quote)
            equity += fill.notional
            unrealized += fill.notional - D(p['cost_basis'])
        except (KeyError, ValueError):
            missing.append(key)
    return (None, None, missing) if missing else (equity, unrealized, [])


def heartbeat_gaps(directory, start, end):
    path = directory / 'heartbeat.csv'
    points = [parse_time(start)]
    if path.exists():
        with path.open(encoding='utf-8', newline='') as stream:
            points += [parse_time(r['timestamp']) for r in csv.DictReader(stream)]
    points.append(parse_time(end))
    gaps = [dict(start=a.isoformat(), end=b.isoformat(), seconds=(b-a).total_seconds())
            for a, b in zip(points, points[1:]) if (b-a).total_seconds() > 360]
    return gaps


def report(directory, broker, telemetry, manifest, now):
    state = broker.state
    engine = state['strategy_state']
    equity, unrealized, missing = mark_bid(state, broker.markets)
    result = metrics(state, engine, equity)
    closed = completed_positions(state['trades'])
    trades = state['trades']
    signals, rejected = broker.storage.db.execute('SELECT COUNT(*), COALESCE(SUM(rejected),0) FROM signals').fetchone()
    curve = [D(50)]
    path = directory / 'valuations.csv'
    if path.exists():
        with path.open(encoding='utf-8', newline='') as stream:
            curve += [D(r['equity']) if r['equity'] else None for r in csv.DictReader(stream)]
    dd, ddpct = drawdown(curve + [equity])
    by_strategy = {}
    for name in sorted({t.get('strategy') or 'unknown' for t in trades}):
        subset = [t for t in trades if (t.get('strategy') or 'unknown') == name]
        rounds = [t for t in closed if t['strategy'] == name]
        by_strategy[name] = dict(entries=sum(t['side']=='BUY' for t in subset), exits=sum(t['side']=='SELL' for t in subset),
            realized_pnl=str(sum((D(t['realized_pnl']) for t in subset), D(0))),
            win_rate=str(D(sum(t['net']>0 for t in rounds))/len(rounds)) if rounds else None)
        keys = {t['market_id'] + ':' + t['outcome'] for t in subset}
        owned = {k: p for k,p in state['positions'].items() if k in keys and
                 engine['positions_meta'].get(k, {}).get('strategy') == name}
        _, upnl, _ = mark_bid(dict(cash_balance='0', positions=owned), broker.markets)
        by_strategy[name]['unrealized_pnl'] = str(upnl) if upnl is not None else None
        by_strategy[name]['PnL'] = str(D(by_strategy[name]['realized_pnl'])+upnl) if upnl is not None else None
    benchmark = engine['benchmark']
    bp = benchmark.get('position')
    beq, _, _ = mark_bid(dict(cash_balance=benchmark['cash'], positions={
        'benchmark': dict(bp, cost_basis='0')} if bp else {}), broker.markets)
    hypothetical, _ = liquidation(broker, broker.markets)
    gaps = heartbeat_gaps(directory, manifest['start_time'], now.isoformat())
    elapsed = (now-parse_time(manifest['start_time'])).total_seconds()
    result.update(experiment_id=manifest['experiment_id'], start_time=manifest['start_time'],
        end_time=now.isoformat(), scheduled_end_time=manifest['scheduled_end_time'], elapsed_seconds=elapsed,
        elapsed_hours=elapsed/3600, starting_equity='50.00', ending_equity_mark_to_bid=str(equity) if equity is not None else None,
        equity=str(equity) if equity is not None else None, cash=state['cash_balance'], realized_pnl=state['realized_pnl'],
        unrealized_pnl=str(unrealized) if unrealized is not None else None, return_pct=result['total_return_pct'],
        total_entries=sum(t['side']=='BUY' for t in trades), total_exits=sum(t['side']=='SELL' for t in trades),
        win_rate=result['win_rate'] if closed else None, average_win=result['average_win'] if result['winning_trades'] else None,
        average_loss=result['average_loss'] if result['losing_trades'] else None, expectancy=result['expectancy'] if closed else None,
        max_drawdown_usd=str(dd), max_drawdown_pct=str(ddpct), fees_total=result['fees'], slippage_total=result['slippage_cost'],
        signals_generated=engine['counters']['signals_generated'], signal_evaluation_rows=signals, signals_rejected=rejected,
        markets_seen=len(engine['seen_markets']), markets_eligible=len(engine['eligible_markets']),
        API_requests=telemetry['API_requests'], API_errors=telemetry['API_errors'],
        downtime_seconds=telemetry['downtime_seconds'], restart_count=telemetry['restart_count'],
        heartbeat_gaps=gaps, continuity_verified=not gaps and telemetry['restart_count']==0 and telemetry['downtime_seconds']==0,
        by_strategy=by_strategy, benchmark_return=str((beq-D(50))/D(50)*100) if beq is not None else None,
        benchmark_return_basis='mark-to-bid depth before exit fee, percent',
        benchmark_phase2=benchmark, hypothetical_immediate_liquidation=str(hypothetical) if hypothetical is not None else None,
        valuation='executable full SELL bid-depth VWAP before exit fee; stale/insufficient book => null',
        unresolved_positions=missing, unsupported_features=read_json(directory/'config_snapshot.json')['unsupported_features'],
        downtime_definition='observed gaps between persisted worker pulses above 15 seconds; conservative observable estimate',
        strategy_attribution='combined strategy labels preserved; no double counting across component strategies')
    return result


def integrity(directory, root, broker, final):
    checks = {}
    for name, fn in [
        ('initial_equity_50', lambda: read_json(directory/'manifest.json')['starting_equity']=='50.00'),
        ('config_and_code_unchanged', lambda: check_frozen(directory, root)),
        ('cash_position_realized_ledger', lambda: validate_state(broker.state) is None),
        ('no_deleted_or_altered_trades', lambda: broker.storage.witness(broker.state)),
        ('paper_only_static_audit', lambda: static_audit(root)['result']=='PASS'),
        ('PAPER_mode', lambda: broker.state['mode']=='PAPER_ONLY'),
        ('trade_ids_unique', lambda: len({t['trade_id'] for t in broker.state['trades']})==len(broker.state['trades'])),
        ('timestamps_ordered', lambda: all(a['timestamp']<=b['timestamp'] for a,b in zip(broker.state['trades'],broker.state['trades'][1:]))),
        ('equity_identity', lambda: final['equity'] is None or
            abs(D(final['equity'])-D(50)-D(final['realized_pnl'])-D(final['unrealized_pnl']))<=D('1e-18'))]:
        try:
            checks[name] = dict(passed=bool(fn()))
        except Exception as exc:
            checks[name] = dict(passed=False, error=str(exc))
    audit = dict(timestamp=timestamp(), result='PASS' if all(c['passed'] for c in checks.values()) else 'FAIL',
                 checks=checks, unavailable_equity=final['equity'] is None,
                 limitation='Local append-only witnesses detect accidental changes, not coordinated malicious rewriting of all local files.')
    write_json(directory/'verification'/'phase3_integrity_report.json', audit)
    return audit

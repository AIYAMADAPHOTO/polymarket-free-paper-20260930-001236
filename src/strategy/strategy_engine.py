from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
import json
import logging

from ..models import parse_time, utcnow
from ..execution import liquidation
from ..benchmark import start_benchmark, update_benchmark
from ..performance import metrics
from ..storage import atomic_write
from .market_filter import MarketFilter
from .signals import observation, generate, combine, signal_id
from .risk_manager import RiskManager
from .position_sizer import PositionSizer
from .exit_manager import ExitManager


def validate_engine_state(state):
    required = {'version', 'start_time', 'starting_equity', 'start_trade_count', 'history', 'processed_signals',
                'positions_meta', 'daily', 'equity_curve', 'benchmark', 'counters', 'seen_markets',
                'eligible_markets', 'episode_latched', 'active_flags', 'decision_sequence', 'errors', 'warnings', 'config'}
    if not isinstance(state, dict) or not required <= set(state) or state['version'] != 1:
        raise ValueError('invalid strategy state schema')
    parse_time(state['start_time'])
    if not Decimal(state['starting_equity']).is_finite() or Decimal(state['starting_equity']) <= 0:
        raise ValueError('invalid strategy starting equity')
    for name in ('history', 'positions_meta', 'daily', 'benchmark', 'counters', 'episode_latched', 'active_flags'):
        if not isinstance(state[name], dict):
            raise ValueError('invalid strategy state ' + name)
    for rows in state['history'].values():
        previous = None
        for row in rows:
            time = parse_time(row['timestamp'])
            if previous is not None and time <= previous:
                raise ValueError('strategy history not chronological')
            previous = time
            if not 0 < Decimal(row['bid']) <= Decimal(row['ask']) < 1:
                raise ValueError('invalid history price')
    for value in state['counters'].values():
        if not isinstance(value, int) or value < 0:
            raise ValueError('invalid strategy counter')


class StrategyEngine:
    def __init__(self, broker, storage, config, markets, now=None):
        self.broker, self.storage, self.config = broker, storage, config.validate()
        self.log = logging.getLogger('paperbot')
        self.filter, self.risk = MarketFilter(config), RiskManager(config)
        self.sizer, self.exits = PositionSizer(config), ExitManager(config)
        now = now or utcnow()
        stored = broker.state.get('strategy_state')
        settings = {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(config).items()}
        if stored:
            validate_engine_state(stored)
            self.state = stored
            self.log.info('Strategy state restored cycles=%s', stored['counters']['cycles'])
            if stored['config'] != settings:
                self.state['warnings'].append('Config changed at ' + now.isoformat() + '; history and losses preserved')
                self.state['config'] = settings
        else:
            equity, missing = liquidation(broker, markets)
            if equity is None:
                raise ValueError('Cannot establish Phase 2 baseline with unpriced positions: ' + str(missing))
            self.state = dict(version=1, start_time=now.isoformat(), starting_equity=str(equity),
                              start_trade_count=len(broker.state['trades']), history={}, processed_signals=[],
                              positions_meta={}, daily={'date': now.date().isoformat(), 'starting_equity': str(equity)},
                              equity_curve=[], benchmark=start_benchmark(equity, now),
                              counters=dict(cycles=0, observations=0, signal_evaluations=0, signals_generated=0,
                                            paper_entries=0, paper_exits=0, rejected_orders=0),
                              seen_markets=[], eligible_markets=[], episode_latched={}, active_flags={},
                              decision_sequence=0, errors=[], warnings=[], config=settings)
            # Existing open Phase 1 positions are retained and assigned truthful legacy metadata.
            for key, pos in broker.get_positions().items():
                buys = [t for t in broker.state['trades'] if t['market_id'] == pos['market_id'] and
                        t['outcome'] == pos['outcome'] and t['side'] == 'BUY']
                last = buys[-1]
                self.state['positions_meta'][key] = dict(entry_time=last['timestamp'],
                    entry_bid=None, entry_ask=last['simulated_fill_price'], entry_spread=last['spread'],
                    entry_liquidity='0', strategy='phase1/manual', signal_score=None, combined_score=None,
                    risk_budget=str(Decimal(pos['cost_basis'])))
        broker.save_strategy_state(self.state)

    def audit(self, now, market, outcome, action, reasons, signals=None, score=None, risk=None, features=None):
        self.state['decision_sequence'] += 1
        identity = str(self.state['decision_sequence'])
        row = dict(timestamp=now.isoformat(), decision_id=identity, market_id=market.market_id,
                   title=market.title, outcome=outcome, action=action, reason=';'.join(reasons),
                   signals=json.dumps([dict(strategy=s.strategy, signal_score=str(s.score), reason=s.reason,
                                            features=s.features) for s in signals or []], ensure_ascii=False),
                   combined_score=str(score) if score is not None else None,
                   risk_check_result=risk, features=json.dumps(features or {}, ensure_ascii=False))
        self.storage.append_csv('strategy_decisions.csv', row)
        return identity

    def observe(self, market, outcome, now):
        key = market.market_id + ':' + outcome
        quote = market.quotes.get(outcome)
        if quote is None:
            return
        try:
            quote.require_fresh(self.config.max_data_age_seconds)
        except ValueError:
            return
        row = observation(market, outcome, self.config)
        if row is None or parse_time(row['timestamp']) > now:
            return
        history = self.state['history'].setdefault(key, [])
        if history and (parse_time(row['timestamp']) - parse_time(history[-1]['timestamp'])).total_seconds() < self.config.sample_min_seconds:
            return
        history.append(row)
        self.state['history'][key] = [r for r in history if
            0 <= (now - parse_time(r['timestamp'])).total_seconds() <= self.config.history_seconds]
        self.state['counters']['observations'] += 1

    def process(self, market_list, now=None):
        now = now or utcnow()
        markets = {m.market_id: m for m in market_list}
        self.broker.mark(market_list)
        self.state['counters']['cycles'] += 1
        equity, missing = liquidation(self.broker, markets)
        if equity is not None and self.state['daily']['date'] != now.date().isoformat():
            self.state['daily'] = dict(date=now.date().isoformat(), starting_equity=str(equity))
        eligible, candidates, scores = [], [], {}
        for market in market_list:
            if market.market_id not in self.state['seen_markets']:
                self.state['seen_markets'].append(market.market_id)
            for outcome in ('YES', 'NO'):
                key = market.market_id + ':' + outcome
                result = self.filter.evaluate(market, outcome, now)
                self.observe(market, outcome, now)
                signals = generate(self.state['history'].get(key, []), now, self.config)
                score, active = combine(signals, self.config)
                scores[key] = score
                self.state['counters']['signal_evaluations'] += len(signals)
                active_now = score >= self.config.min_combined_score
                if active_now and not self.state['active_flags'].get(key, False):
                    self.state['counters']['signals_generated'] += 1
                self.state['active_flags'][key] = active_now
                if not active_now:
                    self.state['episode_latched'][key] = False
                if not result.eligible:
                    self.audit(now, market, outcome, 'filtered', result.reasons, signals, score, features=result.features)
                    continue
                eligible.append((market, outcome))
                if market.market_id not in self.state['eligible_markets']:
                    self.state['eligible_markets'].append(market.market_id)
                if active_now:
                    candidates.append((score, market, outcome, signals, active, result.features))
                else:
                    self.audit(now, market, outcome, 'no_entry', ['signal_below_threshold'], signals, score, features=result.features)
        self.state['benchmark'] = update_benchmark(self.state['benchmark'], eligible, markets, self.config)
        self.process_exits(markets, scores, now)
        for score, market, outcome, signals, active, features in sorted(candidates, key=lambda c: (-c[0], int(c[1].market_id), c[2])):
            key = market.market_id + ':' + outcome
            history = self.state['history'].get(key, [])
            if not history:
                continue
            identity = signal_id(market.market_id, outcome, active, history[-1])
            equity, _ = liquidation(self.broker, markets)
            risk = self.risk.check(self.broker.state, self.state, market.market_id, identity, equity, now)
            reasons = list(risk.reasons)
            if self.state['episode_latched'].get(key):
                reasons.append('signal_episode_already_consumed')
            if reasons:
                self.audit(now, market, outcome, 'risk_rejected', reasons, signals, score, risk='rejected', features=features)
                continue
            sizing = self.sizer.size(self.broker.state, equity, market, outcome)
            if sizing.quantity <= 0:
                self.audit(now, market, outcome, 'sizing_rejected', [sizing.reason], signals, score, risk='passed', features=features)
                continue
            reasons = [s.reason for s in active]
            decision_id = self.audit(now, market, outcome, 'entry_candidate', reasons, signals, score, risk='passed', features=features)
            quote = market.quotes[outcome]
            context = dict(strategy='+'.join(s.strategy for s in active), signal_score=str(max(s.score for s in active)),
                           combined_score=str(score), entry_reason=';'.join(reasons), exit_reason='', holding_seconds='0',
                           entry_bid=str(quote.bid), entry_ask=str(quote.ask), entry_spread=str(quote.spread),
                           momentum=next((s.features.get('momentum') for s in signals if s.strategy == 'momentum'), None),
                           order_book_imbalance=features.get('imbalance'), liquidity=str(market.liquidity), volume=str(market.volume),
                           risk_check_result='passed', decision_id=decision_id, risk_budget=str(sizing.budget))

            def commit_entry(candidate, trade):
                updated = deepcopy(self.state)
                updated['processed_signals'].append(identity)
                updated['episode_latched'][key] = True
                updated['counters']['paper_entries'] += 1
                updated['positions_meta'][key] = dict(entry_time=trade['timestamp'], entry_bid=str(quote.bid),
                    entry_ask=str(quote.ask), entry_spread=str(quote.spread), entry_liquidity=str(market.liquidity),
                    strategy=context['strategy'], signal_score=context['signal_score'], combined_score=str(score),
                    risk_budget=str(sizing.budget))
                candidate['strategy_state'] = updated

            try:
                limit = min(Decimal('.999999'), quote.ask * (1 + self.config.max_entry_slippage_fraction))
                self.broker.buy(market, outcome, sizing.quantity, requested_price=limit,
                                reason=context['entry_reason'], trade_context=context, state_transform=commit_entry)
                self.state = self.broker.state['strategy_state']
            except ValueError as exc:
                self.state['counters']['rejected_orders'] += 1
                self.audit(now, market, outcome, 'entry_rejected', [str(exc)], signals, score, risk='passed')
        equity, missing = liquidation(self.broker, markets)
        self.state['equity_curve'].append(dict(timestamp=now.isoformat(), equity=str(equity) if equity is not None else None))
        self.broker.save_strategy_state(self.state)
        self.broker.record_portfolio()
        report = metrics(self.broker.state, self.state, equity)
        report.update(benchmark=deepcopy(self.state['benchmark']), unpriced_positions=missing,
                      counters=self.state['counters'], as_of=now.isoformat())
        atomic_write(self.storage.directory / 'performance.json', json.dumps(report, indent=2, ensure_ascii=False) + '\n')
        self.log.info('Phase2 cycle=%s seen=%s eligible=%s signals=%s entries=%s exits=%s equity=%s',
                      self.state['counters']['cycles'], len(markets), len({m.market_id for m, _ in eligible}),
                      self.state['counters']['signals_generated'], self.state['counters']['paper_entries'],
                      self.state['counters']['paper_exits'], equity)
        return report

    def process_exits(self, markets, scores, now):
        for key, position in self.broker.get_positions().items():
            metadata = self.state['positions_meta'].get(key)
            if metadata is None:
                self.state['warnings'].append('Unmanaged position: ' + key)
                continue
            market = markets.get(position['market_id'])
            opposite = 'NO' if position['outcome'] == 'YES' else 'YES'
            reasons, net = self.exits.evaluate(position, metadata, market, now,
                                              scores.get(position['market_id'] + ':' + opposite, Decimal('0')))
            if not reasons:
                continue
            if market is None:
                self.log.warning('Exit held: market unavailable %s', key)
                continue
            q = market.quotes.get(position['outcome'])
            identity = self.audit(now, market, position['outcome'], 'exit_candidate', reasons, risk='reduce_only')
            context = dict(strategy=metadata['strategy'], signal_score=metadata['signal_score'],
                           combined_score=metadata['combined_score'], entry_reason='', exit_reason=';'.join(reasons),
                           holding_seconds=str((now - parse_time(metadata['entry_time'])).total_seconds()),
                           entry_bid=metadata['entry_bid'], entry_ask=metadata['entry_ask'], entry_spread=metadata['entry_spread'],
                           exit_bid=str(q.bid) if q and q.bid is not None else None,
                           exit_ask=str(q.ask) if q and q.ask is not None else None,
                           exit_spread=str(q.spread) if q and q.spread is not None else None,
                           liquidity=str(market.liquidity) if market.liquidity is not None else None,
                           volume=str(market.volume) if market.volume is not None else None,
                           decision_id=identity, risk_check_result='reduce_only', risk_budget=metadata['risk_budget'])

            def commit_exit(candidate, trade):
                updated = deepcopy(self.state)
                updated['counters']['paper_exits'] += 1
                if Decimal(trade['position_after']) == 0:
                    updated['positions_meta'].pop(key, None)
                candidate['strategy_state'] = updated

            try:
                self.broker.sell(market, position['outcome'], position['quantity'], reason=context['exit_reason'],
                                 trade_context=context, state_transform=commit_exit, allow_partial=True)
                self.state = self.broker.state['strategy_state']
            except ValueError as exc:
                self.state['counters']['rejected_orders'] += 1
                self.audit(now, market, position['outcome'], 'exit_rejected_position_retained', [str(exc)], risk='reduce_only')

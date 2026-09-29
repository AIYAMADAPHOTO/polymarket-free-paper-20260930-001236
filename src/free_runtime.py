"""48-hour zero-cost paper runtime modeled on the viral $50 Polymarket post."""
from __future__ import annotations

from decimal import Decimal
import json
import logging
import os
from pathlib import Path
import time

from .broad_scanner import BroadMarketScanner, candidate_from_raw
from .free_strategy import FreeFairValueKellyStrategy, build_fair_value_prompt
from .free_evidence import FreeEvidenceCollector
from .gemini_free import GeminiFreeClient, GeminiFreeError, FreeQuotaExhausted
from .market_scanner import MarketScanner
from .models import parse_time, timestamp, utcnow
from .paper_broker import PaperBroker
from .polymarket_client import PolymarketClient, ApiError
from .storage import Storage, atomic_write


def _write_json(path, data):
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n')


def _manifest(directory, config):
    path = directory / 'free_experiment_manifest.json'
    if path.exists():
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('config') != config.public_dict():
            raise ValueError('Experiment config changed; use a new data directory')
        return data
    start = utcnow()
    data = {
        'experiment': 'zero_cost_original_post_reproduction',
        'mode': 'PAPER_ONLY',
        'starting_equity': '50.00',
        'start_time': start.isoformat(),
        'scheduled_end_time': (start + __import__('datetime').timedelta(hours=config.duration_hours)).isoformat(),
        'config': config.public_dict(),
        'cost_policy': {
            'paid_fallbacks': False,
            'gemini_project_billing_must_be_disabled': True,
            'max_gemini_calls_per_day': config.max_gemini_calls_per_day,
        },
    }
    _write_json(path, data)
    return data


def _candidate_lookup(client, candidates, held_ids):
    by_id = {c.market_id: c for c in candidates}
    for market_id in held_ids - set(by_id):
        try:
            by_id[market_id] = candidate_from_raw(client.market(market_id))
        except (ApiError, ValueError, KeyError, TypeError):
            pass
    return by_id


def _potential_edge(candidate, fair):
    if fair is None or fair.fair_yes is None:
        return Decimal('-1')
    yes = candidate.market.prices.get('YES')
    no = candidate.market.prices.get('NO')
    edges = []
    if yes is not None:
        edges.append(fair.fair_yes - yes)
    if no is not None:
        edges.append((Decimal('1') - fair.fair_yes) - no)
    return max(edges) if edges else Decimal('-1')


def _append_fair_value(storage, candidate, fair, model, grounding_sources, now):
    storage.append_csv('free_fair_values.csv', {
        'timestamp': now.isoformat(),
        'market_id': candidate.market_id,
        'question': candidate.title,
        'market_yes_price': str(candidate.yes_price) if candidate.yes_price is not None else None,
        'fair_yes': str(fair.fair_yes) if fair and fair.fair_yes is not None else None,
        'confidence': str(fair.confidence) if fair else None,
        'source_quality': str(fair.source_quality) if fair else None,
        'skip_reason': fair.skip_reason if fair else 'ai_unavailable',
        'rationale': fair.rationale if fair else '',
        'model': model,
        'grounding_sources': json.dumps(grounding_sources[:100], ensure_ascii=False),
    })


def _report(directory, broker, strategy, manifest, status='RUNNING'):
    value = broker.valuation()
    state = broker.state
    equity = value['equity']
    start = Decimal('50.00')
    pnl = None if equity is None else Decimal(equity) - start
    report = {
        'status': status,
        'timestamp': timestamp(),
        'start_time': manifest['start_time'],
        'scheduled_end_time': manifest['scheduled_end_time'],
        'starting_equity': '50.00',
        'cash': state['cash_balance'],
        'equity_mark_to_bid_before_exit_fee': equity,
        'unrealized_pnl_mark_to_bid_before_exit_fee': value['unrealized_pnl'],
        'realized_pnl': state['realized_pnl'],
        'net_pnl_mark_to_bid': str(pnl) if pnl is not None else None,
        'return_pct_mark_to_bid': str(pnl / start * 100) if pnl is not None else None,
        'open_positions': broker.get_positions(),
        'trade_count': len(state['trades']),
        'counters': strategy.state['counters'],
        'last_model': strategy.state.get('last_model'),
        'valuation_status': value['valuation_status'],
        'unpriced_positions': value['unpriced_positions'],
        'paid_api_fallback_used': False,
        'real_order_code_used': False,
    }
    _write_json(directory / 'free_status.json', report)
    return report


def run_cycle(directory, config, client, gemini, broad, enrich_scanner, broker, strategy, storage, evidence_collector=None):
    log = logging.getLogger('paperbot.free')
    now = utcnow()
    candidates = broad.scan()
    held_ids = {p['market_id'] for p in broker.get_positions().values()}
    by_id = _candidate_lookup(client, candidates, held_ids)
    selected, rejected = broad.prefilter(list(by_id.values()), held_ids)
    strategy.begin_cycle(broad.stats.get('inspected', 0), len(selected), rejected)

    storage.append_csv('free_scan_summary.csv', {
        'timestamp': now.isoformat(),
        'inspected': broad.stats.get('inspected', 0),
        'accepted_metadata': broad.stats.get('accepted_metadata', 0),
        'selected_for_ai': len(selected),
        'held_markets': len(held_ids),
        'political_excluded': sum(1 for _, reasons in rejected if 'political_market_excluded' in reasons),
        'pages': broad.stats.get('pages', 0),
    })

    evidence, evidence_stats = {}, {'fresh_fetches': 0, 'errors': [], 'markets_with_evidence': 0}
    if selected and evidence_collector is not None:
        evidence, evidence_stats = evidence_collector.collect(selected)
        for err in evidence_stats.get('errors', []):
            storage.append_csv('free_evidence_errors.csv', {'timestamp': now.isoformat(), **err})

    values, sources, model = {}, [], None
    if selected:
        prompt = build_fair_value_prompt(selected, now, evidence=evidence)
        try:
            values, sources, model = gemini.evaluate(prompt, [c.market_id for c in selected])
            strategy.record_ai(values, model, sources)
        except (FreeQuotaExhausted, GeminiFreeError) as exc:
            log.warning('AI cycle skipped without paid fallback: %s', exc)
            storage.append_csv('free_ai_failures.csv', {
                'timestamp': now.isoformat(), 'error': str(exc), 'paid_fallback': False})

    for candidate in selected:
        _append_fair_value(storage, candidate, values.get(candidate.market_id), model, sources, now)

    # Enrich only held positions and AI-estimated candidates that are even plausibly near the 8% edge.
    enrich_ids = set(held_ids)
    for candidate in selected:
        fair = values.get(candidate.market_id)
        if fair and fair.fair_yes is not None and _potential_edge(candidate, fair) >= config.min_edge - Decimal('0.02'):
            enrich_ids.add(candidate.market_id)

    enriched = {}
    for market_id in enrich_ids:
        candidate = by_id.get(market_id)
        if not candidate:
            continue
        try:
            enriched[market_id] = enrich_scanner.enrich(candidate.market)
        except (ApiError, ValueError, KeyError, TypeError) as exc:
            strategy.reject(market_id, 'CLOB enrichment failed: ' + str(exc), now)
    broker.mark(enriched.values())

    # Reduce risk first. Fair-value exits use the fresh AI estimate when available;
    # time/stop guards still work during an AI outage.
    exits = []
    for market_id in held_ids:
        market = enriched.get(market_id)
        if market:
            exits.extend(strategy.evaluate_exit(market, values.get(market_id), sources, model, now))

    entries = []
    for candidate in selected:
        market = enriched.get(candidate.market_id)
        fair = values.get(candidate.market_id)
        if not market or not fair or fair.fair_yes is None:
            continue
        try:
            trade, reason = strategy.evaluate_entry(candidate, market, fair, sources, model, now)
            if trade:
                entries.append(trade)
            elif reason:
                strategy.reject(candidate.market_id, reason, now)
        except ValueError as exc:
            strategy.reject(candidate.market_id, str(exc), now)

    value = broker.valuation()
    storage.append_csv('free_heartbeat.csv', {
        'timestamp': timestamp(),
        'markets_inspected': broad.stats.get('inspected', 0),
        'selected_for_ai': len(selected),
        'ai_model': model,
        'evidence_markets': evidence_stats.get('markets_with_evidence', 0),
        'evidence_fresh_fetches': evidence_stats.get('fresh_fetches', 0),
        'entries': len(entries),
        'exits': len(exits),
        'cash': str(broker.get_cash_balance()),
        'equity': value['equity'],
        'open_positions': len(broker.get_positions()),
    })
    return {
        'timestamp': timestamp(),
        'scan': broad.stats,
        'selected_for_ai': len(selected),
        'ai_model': model,
        'evidence_markets': evidence_stats.get('markets_with_evidence', 0),
        'evidence_fresh_fetches': evidence_stats.get('fresh_fetches', 0),
        'entries': len(entries),
        'exits': len(exits),
        'cash': str(broker.get_cash_balance()),
        'equity': value['equity'],
        'open_positions': len(broker.get_positions()),
    }


def run(directory, config, once=False, shutdown=None, opener=None):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    manifest = _manifest(directory, config)
    end = parse_time(manifest['scheduled_end_time'])
    log = logging.getLogger('paperbot.free')
    client = PolymarketClient()
    with Storage(directory) as storage:
        broker = PaperBroker(storage, __import__('src.execution', fromlist=['DepthFill']).DepthFill())
        broad = BroadMarketScanner(client, config)
        enrich = MarketScanner(client, storage, max_markets=1)
        gemini = GeminiFreeClient(os.getenv('GEMINI_API_KEY', ''), config, directory, opener=opener) if opener else \
                 GeminiFreeClient(os.getenv('GEMINI_API_KEY', ''), config, directory)
        strategy = FreeFairValueKellyStrategy(broker, storage, config)
        evidence = FreeEvidenceCollector(config, directory)
        report = _report(directory, broker, strategy, manifest)
        while True:
            if shutdown and shutdown.reason:
                _report(directory, broker, strategy, manifest, 'INTERRUPTED')
                return 0
            if utcnow() >= end:
                _report(directory, broker, strategy, manifest, 'COMPLETED')
                return 0
            try:
                result = run_cycle(directory, config, client, gemini, broad, enrich, broker, strategy, storage, evidence)
                log.info('FREE cycle %s', result)
                _write_json(directory / 'free_last_cycle.json', result)
            except (ApiError, ValueError, KeyError, TypeError) as exc:
                log.exception('FREE cycle failed; state retained; no paid fallback')
                strategy.state['errors'].append({'timestamp': timestamp(), 'error': str(exc)})
                strategy._save()
                storage.append_csv('free_runtime_errors.csv', {'timestamp': timestamp(), 'error': str(exc)})
            report = _report(directory, broker, strategy, manifest)
            if once:
                return 0
            sleep_until = min(end, utcnow() + __import__('datetime').timedelta(seconds=config.scan_interval_seconds))
            while utcnow() < sleep_until:
                if shutdown and shutdown.reason:
                    _report(directory, broker, strategy, manifest, 'INTERRUPTED')
                    return 0
                time.sleep(min(1, max(0, (sleep_until - utcnow()).total_seconds())))

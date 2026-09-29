import json
import time
import logging

from config import StrategyConfig
from .models import timestamp, utcnow
from .polymarket_client import ApiError
from .strategy.strategy_engine import StrategyEngine
from .storage import atomic_write


def run_strategy(args, broker, scanner, storage):
    log = logging.getLogger('paperbot')
    config = StrategyConfig().validate()
    engine = None
    start = time.monotonic()
    deadline = start + args.run_seconds if args.run_seconds else None
    cycles = failures = 0
    while True:
        if deadline and time.monotonic() >= deadline:
            break
        cycles += 1
        try:
            markets = scanner.scan()
            by_id = {m.market_id: m for m in markets}
            extra = {p['market_id'] for p in broker.get_positions().values()}
            saved = engine.state if engine else broker.state.get('strategy_state', {})
            benchmark = saved.get('benchmark', {}).get('position')
            if benchmark:
                extra.add(benchmark['market_id'])
            for mid in extra - set(by_id):
                try:
                    by_id[mid] = scanner.fetch_market(mid)
                except (ApiError, ValueError, KeyError, TypeError) as exc:
                    log.warning('Held/benchmark market unavailable %s: %s', mid, exc)
            if engine is None:
                engine = StrategyEngine(broker, storage, config, by_id)
            report = engine.process(list(by_id.values()))
            failures = 0
            result = dict(timestamp=timestamp(), status='ok', mode='PAPER', runtime_seconds=time.monotonic() - start,
                          scan=scanner.stats, counters=engine.state['counters'], current_equity=report['current_equity'])
        except (ApiError, ValueError, KeyError, TypeError) as exc:
            failures += 1
            log.exception('Phase 2 cycle failed; no fabricated data; retrying')
            broker.mark([])
            if engine:
                engine.state['errors'].append(dict(timestamp=timestamp(), error=str(exc)))
                engine.state['equity_curve'].append(dict(timestamp=timestamp(), equity=None))
                broker.save_strategy_state(engine.state)
            result = dict(timestamp=timestamp(), status='error', mode='PAPER', error=str(exc))
        atomic_write(storage.directory / 'last_run.json', json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(result, ensure_ascii=True), flush=True)
        if args.once or (args.cycles and cycles >= args.cycles):
            break
        delay = min(300, args.interval * 2 ** min(failures, 3))
        if deadline:
            delay = min(delay, max(0, deadline - time.monotonic()))
        time.sleep(delay)
    session = dict(started_at=None, finished_at=timestamp(), elapsed_seconds=time.monotonic() - start,
                   cycles=cycles, last_status=result['status'] if cycles else 'no_cycles',
                   engine_start_time=engine.state['start_time'] if engine else None)
    atomic_write(storage.directory / 'phase2_session.json', json.dumps(session, indent=2) + '\n')
    return 0 if engine is not None and result['status'] == 'ok' else 1

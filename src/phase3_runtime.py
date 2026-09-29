"""Deadline-bound, restartable observer using unchanged Phase 2 strategies."""
import ctypes
import json
import logging
import os
import shutil
import time
from datetime import timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from config import StrategyConfig, SCAN_INTERVAL_SECONDS, MAX_MARKETS
from .models import utcnow, timestamp, parse_time
from .paper_broker import PaperBroker
from .polymarket_client import PolymarketClient, ApiError
from .market_scanner import MarketScanner
from .execution import DepthFill
from .storage import StorageError
from .phase3_audit import (EvidenceStorage, RecordedEngine, read_json, write_json, check_frozen,
                           mark_bid, report, integrity)


class DeadlineReached(Exception):
    pass


class StopRequested(Exception):
    pass


class GuardedBroker(PaperBroker):
    def __init__(self, storage, guard):
        self.guard = guard
        super().__init__(storage, DepthFill())

    def _trade(self, *args, **kwargs):
        self.guard()
        result = super()._trade(*args, **kwargs)
        self.storage.record_fill(self.state['trades'][-1])
        return result


class TrackedOpener:
    def __init__(self, opener, telemetry, persist, guard=lambda: None):
        self.opener, self.telemetry, self.persist, self.guard = opener, telemetry, persist, guard

    def open(self, request, timeout):
        self.guard()
        t = self.telemetry
        t['API_requests'] += 1
        self.persist()
        started = time.time()
        try:
            response = self.opener.open(request, timeout=timeout)
        except Exception:
            t['API_errors'] += 1
            t['API_status'] = 'error'
            self.persist()
            raise
        date = response.headers.get('Date')
        if date:
            server = parsedate_to_datetime(date).timestamp()
            offset = server - (started + time.time())/2
            t['clock_offset_seconds'] = offset
            if abs(offset) > 30:
                response.close()
                raise StorageError('INVALID: server/system clock disagreement exceeds 30 seconds')
        return ObservedResponse(response, t, self.persist)


class ObservedResponse:
    """Count body/JSON failures as request errors, including retried responses."""
    def __init__(self, response, telemetry, persist):
        self.response, self.telemetry, self.persist = response, telemetry, persist

    def __enter__(self):
        self.response.__enter__()
        return self

    def __exit__(self, *args):
        return self.response.__exit__(*args)

    def read(self):
        try:
            body = self.response.read()
            json.loads(body.decode('utf-8'))
            if self.telemetry['API_status'] == 'error':
                logging.getLogger('paperbot').info('API RECOVERY public GET body validated')
            self.telemetry['API_status'] = 'ok'
            self.telemetry['last_successful_API_request'] = timestamp()
            self.persist()
            return body
        except Exception:
            self.telemetry['API_errors'] += 1
            self.telemetry['API_status'] = 'error'
            self.persist()
            raise


def empty_telemetry():
    return dict(API_requests=0, API_errors=0, API_status='not_requested', last_successful_API_request=None,
                restart_count=0, downtime_seconds=0.0, last_pulse=None, last_heartbeat=None,
                last_summary=None, cycles=0, markets_scanned=0, worker_starts=0)


def run_worker(directory, root, shutdown=None, deployment=None):
    directory, root = Path(directory), Path(root)
    if (directory/'status.json').exists() and read_json(directory/'status.json')['status'] in ('COMPLETED','INVALID','PREFLIGHT_FAILED'):
        return 3
    if deployment:
        from .vps_support import ArchiveLogHandler
        logging.basicConfig(handlers=[ArchiveLogHandler(directory/'runtime.log'),logging.StreamHandler()],
                            level=deployment['LOG_LEVEL'],format='%(asctime)s %(levelname)s %(message)s',force=True)
    else:
        logging.basicConfig(filename=directory/'runtime.log', level=logging.INFO,
                            format='%(asctime)s %(levelname)s %(message)s', encoding='utf-8', force=True)
    log = logging.getLogger('paperbot')
    manifest = read_json(directory/'manifest.json')
    telemetry_path = directory/'telemetry.json'
    telemetry = read_json(telemetry_path) if telemetry_path.exists() else empty_telemetry()
    process_started = time.monotonic()
    interval = deployment['SCAN_INTERVAL_SECONDS'] if deployment else SCAN_INTERVAL_SECONDS
    last_persist = 0.0
    broker = None
    def persist():
        write_json(telemetry_path, telemetry)
    def status(value, **extra):
        write_json(directory/'status.json', dict(timestamp=timestamp(), status=value, pid=os.getpid(),
            experiment_id=manifest['experiment_id'], **extra))
    try:
        with EvidenceStorage(directory) as storage:
            check_frozen(directory, root)
            if manifest.get('start_time') is None:
                if read_json(directory/'verification'/'preflight.json')['result'] != 'PASS':
                    raise StorageError('Preflight did not pass')
                if (directory/'time_origin.json').exists():
                    manifest = read_json(directory/'time_origin.json')
                else:
                    start = utcnow()
                    end = start + timedelta(hours=48)
                    manifest.update(start_time=start.isoformat(), start_time_local=start.astimezone().isoformat(),
                                    scheduled_end_time=end.isoformat(), scheduled_end_time_local=end.astimezone().isoformat())
                    write_json(directory/'time_origin.json', manifest)
                write_json(directory/'manifest.json', manifest)
            elif read_json(directory/'time_origin.json') != manifest:
                raise StorageError('INVALID: experiment identity/deadline modified')
            start, end = parse_time(manifest['start_time']), parse_time(manifest['scheduled_end_time'])
            if (end-start).total_seconds()!=172800:
                raise StorageError('INVALID: deadline is not 48 hours')
            monotonic_end = time.monotonic() + max(0, (end-utcnow()).total_seconds())
            def guard():
                if utcnow() >= end or time.monotonic() >= monotonic_end:
                    raise DeadlineReached()
                if (directory/'STOP').exists():
                    raise StopRequested()
                if shutdown and shutdown.reason:
                    raise StopRequested()
                check_frozen(directory, root)
            broker = GuardedBroker(storage, guard)
            engine = RecordedEngine(broker, storage, StrategyConfig(), {}, now=start)
            telemetry['restart_count'] += int(telemetry['worker_starts'] > 0)
            telemetry['worker_starts'] += 1
            status('RUNNING', start_time=manifest['start_time'], scheduled_end_time=manifest['scheduled_end_time'])
            log.info('Phase3 worker START id=%s pid=%s original_end=%s', manifest['experiment_id'], os.getpid(), end)
            # Thread-scoped keep-awake request. Cleared on exit; no OS power plan modification.
            awake = bool(ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)) if os.name=='nt' else False
            telemetry['transient_keep_awake'] = awake
            def pulse(force=False):
                nonlocal last_persist
                now = utcnow()
                if not force and time.monotonic()-last_persist < 5:
                    return
                if deployment and shutil.disk_usage(directory).free < 256*1024*1024:
                    raise StorageError('INVALID: disk reserve below 256 MiB; preserve evidence and stop')
                previous = telemetry.get('last_pulse')
                if previous:
                    gap = (now-parse_time(previous)).total_seconds()
                    if gap > 15:
                        telemetry['downtime_seconds'] += gap
                        log.warning('Observed worker pulse gap %.3f seconds (network stall/suspend/restart possible)', gap)
                telemetry['last_pulse'] = now.isoformat()
                last_persist = time.monotonic()
                equity, unrealized, missing = mark_bid(broker.state, broker.markets)
                if force or not telemetry['last_heartbeat'] or (now-parse_time(telemetry['last_heartbeat'])).total_seconds()>=300:
                    storage.append_csv('heartbeat.csv', dict(timestamp=now.isoformat(), process_uptime=time.monotonic()-process_started,
                        API_status=telemetry['API_status'], cash=broker.state['cash_balance'], equity=equity,
                        open_positions=len(broker.get_positions()), markets_scanned=telemetry['markets_scanned'],
                        last_successful_API_request=telemetry['last_successful_API_request'], error_count=telemetry['API_errors'], pid=os.getpid()))
                    telemetry['last_heartbeat'] = now.isoformat()
                persist()
                if not telemetry['last_summary'] or (now-parse_time(telemetry['last_summary'])).total_seconds()>=3600:
                    result = report(directory, broker, telemetry, manifest, now)
                    write_json(directory/'summaries'/('hour_%s.json' % now.strftime('%Y%m%d_%H%M%S')), result)
                    telemetry['last_summary'] = now.isoformat()
                    persist()
            def guarded_pulse():
                guard()
                pulse()
            def wait(seconds):
                until = time.monotonic()+seconds
                while time.monotonic()<until:
                    guarded_pulse()
                    time.sleep(min(1, max(0, until-time.monotonic())))
            client = PolymarketClient(sleep=wait)
            client.opener = TrackedOpener(client.opener, telemetry, persist, guarded_pulse)
            scanner = MarketScanner(client, storage, max_markets=MAX_MARKETS)
            failures = 0
            try:
                pulse(force=True)
                while True:
                    guard()
                    try:
                        markets = scanner.scan()
                        by_id = {m.market_id: m for m in markets}
                        extra = {p['market_id'] for p in broker.get_positions().values()}
                        benchmark = engine.state['benchmark'].get('position')
                        if benchmark:
                            extra.add(benchmark['market_id'])
                        for mid in extra-set(by_id):
                            try:
                                by_id[mid] = scanner.fetch_market(mid)
                            except (ApiError, ValueError, KeyError, TypeError) as exc:
                                log.warning('UNRESOLVED or unavailable held market %s: %s', mid, exc)
                        guard()  # A scan may finish after the deadline; never process late entries.
                        engine.process(list(by_id.values()))
                        equity, unrealized, missing = mark_bid(broker.state, broker.markets)
                        storage.append_csv('valuations.csv', dict(timestamp=timestamp(), equity=equity,
                            unrealized_pnl=unrealized, missing=';'.join(missing)))
                        telemetry['cycles'] += 1
                        telemetry['markets_scanned'] = len(by_id)
                        failures = 0
                        write_json(directory/'live_summary.json', dict(timestamp=timestamp(), mode='PAPER',
                            cycles=telemetry['cycles'], cash=broker.state['cash_balance'], equity=equity,
                            open_positions=len(broker.get_positions()), counters=engine.state['counters'],
                            API_requests=telemetry['API_requests'], API_errors=telemetry['API_errors']))
                        status('RUNNING', cycles=telemetry['cycles'], last_successful_cycle=timestamp())
                    except ApiError as exc:
                        failures += 1
                        log.exception('API cycle failed; clearing valuations and backing off')
                        broker.mark([])
                        telemetry['API_status'] = 'error'
                        engine.state['errors'].append(dict(timestamp=timestamp(), error=str(exc)))
                        broker.save_strategy_state(engine.state)
                    pulse()
                    wait(min(300, interval * 2**min(failures,3)))
            except DeadlineReached:
                # No late API fetch: final marks are fresh, already observed quotes only.
                pulse(force=True)
                final = report(directory, broker, telemetry, manifest, utcnow())
                final['status'] = 'COMPLETED'
                final['valuation_as_of'] = timestamp()
                final['start_time_local'] = manifest['start_time_local']
                final['scheduled_end_time_local'] = manifest['scheduled_end_time_local']
                final['config_hash'] = manifest['config_hash']
                audit = integrity(directory, root, broker, final)
                if audit['result']!='PASS':
                    final['status'] = 'INVALID'
                write_json(directory/'final_report.json', final)
                status(final['status'])
                log.info('Phase3 FINISHED status=%s equity=%s; positions retained, no forced SELL', final['status'], final['equity'])
                return 0
            except StopRequested:
                broker.save_strategy_state(engine.state)
                pulse(force=True)
                reason=shutdown.reason if shutdown and shutdown.reason else 'User stop request'
                write_json(directory/('shutdown_'+utcnow().strftime('%Y%m%dT%H%M%S_%f')+'.json'),
                           dict(timestamp=timestamp(),reason=reason,cash=broker.state['cash_balance'],
                                open_positions=len(broker.get_positions()),trades=len(broker.state['trades']),
                                scheduled_end_time=manifest['scheduled_end_time']))
                status('INTERRUPTED', reason=reason+'; deadline unchanged; not a completed 48h experiment')
                return 0
            finally:
                if os.name=='nt':
                    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    except Exception as exc:
        if isinstance(exc, StorageError) and 'already in use' in str(exc):
            return 3
        log.exception('INVALID: fatal experiment error; no automatic reset or in-place repair')
        status('INVALID', reason=str(exc))
        write_json(directory/'invalid_report.json', dict(timestamp=timestamp(), error=str(exc),
            action='Preserve all evidence. Fix and test project, then create a NEW experiment.'))
        return 2

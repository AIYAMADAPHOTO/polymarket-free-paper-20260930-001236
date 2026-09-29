"""Phase 3 lifecycle CLI. New experiments use a frozen private runtime copy."""
import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from config import validate_trading_mode, StrategyConfig
from src.models import utcnow, timestamp
from src.storage import Storage, StorageError, atomic_write
from src.paper_broker import PaperBroker
from src.execution import DepthFill
from src.market_scanner import MarketScanner
from src.polymarket_client import PolymarketClient
from src.phase3_audit import (EvidenceStorage, sha, sources, snapshot, write_json, read_json,
                              static_audit, check_frozen, RecordedEngine)
from src.phase3_runtime import run_worker, TrackedOpener, empty_telemetry


def legacy_hashes(root):
    files = [p for name in ('data', 'logs', 'verification') for p in (root/name).rglob('*') if p.is_file()]
    return {str(p.relative_to(root)).replace('\\','/'): sha(p) for p in files}


def run_tests(directory):
    sys.path.insert(0, str(ROOT/'tests'))
    suite = unittest.defaultTestLoader.discover(str(ROOT/'tests'))
    def names(group):
        for test in group:
            if isinstance(test, unittest.TestSuite):
                yield from names(test)
            else:
                yield test.id()
    test_ids = list(names(suite))
    with (directory/'verification'/'tests.log').open('w', encoding='utf-8') as stream:
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    data = dict(tests=result.testsRun, passed=result.testsRun-len(result.errors)-len(result.failures)-len(result.skipped),
                failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped), success=result.wasSuccessful(),
                test_ids=test_ids, by_phase={p:sum(t.startswith(p+'.') for t in test_ids) for p in ('test_phase1','test_phase2','test_phase3')})
    write_json(directory/'verification'/'tests.json', data)
    return 0 if result.wasSuccessful() and not result.skipped and result.testsRun>=65 else 1


def preflight(directory):
    checks = {}
    def check(name, action):
        try:
            evidence = action()
            if evidence is False:
                raise ValueError('check returned false')
            checks[name] = dict(passed=True, evidence=evidence)
        except Exception as exc:
            checks[name] = dict(passed=False, error=str(exc))
    validate_trading_mode()
    (directory/'verification').mkdir(exist_ok=True)
    check('all_tests', lambda: subprocess.run([sys.executable, str(ROOT/'phase3.py'), '_tests', str(directory)], cwd=ROOT).returncode==0)
    tested = read_json(directory/'verification'/'tests.json')
    check('original_65_tests', lambda: tested['by_phase']['test_phase1']==26 and tested['by_phase']['test_phase2']==39 and tested['success'])
    check('synthetic_depth_slippage', lambda: tested['success'] and sum('.Phase3DepthTests.' in t for t in tested['test_ids'])==5)
    check('phase3_lifecycle_tests', lambda: tested['success'] and tested['by_phase']['test_phase3']>=33)
    check('PAPER_mode', lambda: validate_trading_mode() is None)
    check('static_read_only', lambda: static_audit(ROOT)['result']=='PASS')
    write_json(directory/'verification'/'read_only_audit.json', static_audit(ROOT))
    check('config_hash_and_frozen_sources', lambda: check_frozen(directory, ROOT))
    check('experiment_directory', lambda: directory.is_dir())
    check('phase2_logic_unchanged', lambda: all(sha(ROOT/name)==expected for name,expected in
          read_json(directory/'verification'/'phase2_source_baseline.json').items()))
    probe = directory/'verification'/'recovery_probe'
    def storage_checks():
        # Real fills and state, then a NEW Python process must restore byte-equivalent balances/positions/PnL.
        sys.path.insert(0, str(ROOT/'tests'))
        from test_phase2 import depth_market
        with EvidenceStorage(probe) as store:
            b = PaperBroker(store, DepthFill())
            m = depth_market()
            b.buy(m, 'YES', '2')
            b.sell(m, 'YES', '1')
            expected = b.state
            write_json(probe/'expected.json', expected)
            atomic_write(probe/'atomic_probe.txt', 'one')
            atomic_write(probe/'atomic_probe.txt', 'two')
            assert (probe/'atomic_probe.txt').read_text()=='two'
        run = subprocess.run([sys.executable, str(ROOT/'phase3.py'), '_recover', str(probe)], cwd=ROOT, capture_output=True, text=True)
        if run.returncode:
            raise ValueError(run.stderr + run.stdout)
        return dict(atomic_write='PASS', cross_process_restart='exact', fictional_probe_trades=2)
    check('storage_atomic_write_restart', storage_checks)
    with EvidenceStorage(directory) as store:
        b = PaperBroker(store, DepthFill())
        check('starting_equity_50', lambda: b.state['cash_balance']=='50.00' and b.state['starting_balance']=='50.00')
        check('zero_positions_and_trades', lambda: not b.get_positions() and not b.state['trades'])
    probe_api = directory/'verification'/'api_probe'
    telemetry = empty_telemetry()
    def api_check():
        client = PolymarketClient()
        client.opener = TrackedOpener(client.opener, telemetry, lambda: None)
        with Storage(probe_api) as store:
            markets = MarketScanner(client, store, max_markets=3).scan()
        assert markets, 'Gamma has no valid active markets'
        books = [(m.market_id, o) for m in markets for o,q in m.quotes.items() if q.bid is not None and q.ask is not None]
        assert books, 'CLOB has no valid order book'
        assert telemetry.get('clock_offset_seconds') is not None, 'API server Date unavailable'
        assert abs(telemetry['clock_offset_seconds'])<=30, 'system clock outside tolerance'
        return dict(Gamma='PASS', CLOB='PASS', markets=[m.market_id for m in markets], books=books,
                    clock_offset_seconds=telemetry['clock_offset_seconds'], API_requests=telemetry['API_requests'])
    check('real_Gamma_CLOB_system_clock', api_check)
    result = dict(timestamp=timestamp(), result='PASS' if all(c['passed'] for c in checks.values()) else 'FAIL', checks=checks)
    write_json(directory/'verification'/'preflight.json', result)
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0 if result['result']=='PASS' else 1


def detached(directory):
    frozen = directory/'runtime'/'phase3.py'
    with (directory/'supervisor.log').open('a', encoding='utf-8') as stream:
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, cwd=str(frozen.parent), close_fds=True)
        if os.name=='nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs['start_new_session'] = True
        process = subprocess.Popen([sys.executable, '-u', str(frozen), '_supervise', str(directory)], **kwargs)
    write_json(directory/'launcher.json', dict(timestamp=timestamp(), supervisor_pid=process.pid))
    return process.pid


def supervise(directory):
    # A second resume cannot start another worker or change its status.
    with Storage(directory/'supervisor_lock'):
        while True:
            status_path = directory/'status.json'
            if status_path.exists() and read_json(status_path)['status'] in ('COMPLETED', 'INVALID', 'INTERRUPTED'):
                return 0
            proc = subprocess.Popen([sys.executable, '-u', str(ROOT/'phase3.py'), '_worker', str(directory)], cwd=ROOT)
            write_json(directory/'processes.json', dict(supervisor_pid=os.getpid(), worker_pid=proc.pid, timestamp=timestamp()))
            code = proc.wait()
            print(timestamp(), 'worker exit', code, flush=True)
            if status_path.exists() and read_json(status_path)['status'] in ('COMPLETED','INVALID','INTERRUPTED'):
                return code
            print(timestamp(), 'abnormal worker termination; restore same experiment after 5 seconds', flush=True)
            time.sleep(5)


def create():
    validate_trading_mode()
    identity = 'phase3_' + utcnow().astimezone().strftime('%Y%m%d_%H%M%S')
    directory = ROOT/'experiments'/identity
    directory.mkdir(parents=True, exist_ok=False)
    (directory/'verification').mkdir()
    baseline = legacy_hashes(ROOT)
    write_json(directory/'verification'/'legacy_before.json', baseline)
    write_json(directory/'verification'/'phase2_source_baseline.json',
               read_json(ROOT/'verification'/'phase2_read_only_audit.json')['source_hashes'])
    runtime = directory/'runtime'
    runtime.mkdir()
    for path in sources(ROOT):
        dest = runtime/path.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
    shutil.copytree(ROOT/'tests', runtime/'tests', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(ROOT/'verification'/'phase2_read_only_audit.json',runtime/'baseline.json')
    for name in ('Dockerfile','docker-compose.yml'):
        if (ROOT/name).exists():shutil.copy2(ROOT/name,runtime/name)
    write_json(directory/'config_snapshot.json', snapshot(runtime))
    manifest = dict(experiment_id=identity, created_at=timestamp(), starting_equity='50.00', start_time=None,
                    scheduled_end_time=None, config_hash=sha(directory/'config_snapshot.json'), python=sys.version,
                    duration_seconds=172800, mode='PAPER')
    write_json(directory/'manifest.json', manifest)
    run = subprocess.run([sys.executable, str(runtime/'phase3.py'), '_preflight', str(directory)], cwd=runtime)
    preserved = baseline==legacy_hashes(ROOT)
    write_json(directory/'verification'/'legacy_preserved.json', dict(passed=preserved, file_count=len(baseline)))
    if run.returncode or not preserved:
        write_json(directory/'status.json', dict(timestamp=timestamp(), status='PREFLIGHT_FAILED'))
        print('NOT STARTED:', directory)
        return 1
    pid = detached(directory)
    print(json.dumps(dict(experiment_id=identity, directory=str(directory), supervisor_pid=pid, launch='dispatched')), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['start','status','stop','resume','_preflight','_tests','_recover','_supervise','_worker'])
    parser.add_argument('directory', nargs='?', type=Path)
    args = parser.parse_args()
    if args.command=='start':
        return create()
    if not args.directory:
        parser.error('explicit experiment directory required')
    directory = args.directory.resolve()
    if args.command=='_tests': return run_tests(directory)
    if args.command=='_preflight': return preflight(directory)
    if args.command=='_recover':
        with EvidenceStorage(directory) as store:
            assert PaperBroker(store, DepthFill()).state==read_json(directory/'expected.json')
        return 0
    if args.command=='_supervise': return supervise(directory)
    if args.command=='_worker': return run_worker(directory, ROOT)
    if args.command=='status':
        for name in ('manifest.json','status.json','migration_disposition.json','processes.json','live_summary.json','telemetry.json'):
            if (directory/name).exists():
                print(name, json.dumps(read_json(directory/name), ensure_ascii=True, indent=2))
        return 0
    if args.command=='stop':
        atomic_write(directory/'STOP', timestamp())
        print('Graceful stop requested; no forced closing, original deadline retained.')
        return 0
    if args.command=='resume':
        if (directory/'migration_disposition.json').exists():
            raise ValueError('ABORTED_BY_USER_MIGRATION: this experiment must not resume')
        current = read_json(directory/'status.json')['status']
        if current in ('COMPLETED','INVALID','PREFLIGHT_FAILED'):
            raise ValueError('This experiment must not be resumed: '+current)
        # Acquiring both locks proves no current supervisor/worker before state mutation.
        with Storage(directory/'supervisor_lock'), Storage(directory):
            check_frozen(directory, directory/'runtime')
            if (directory/'STOP').exists():
                os.replace(directory/'STOP', directory/('stop_acknowledged_'+utcnow().strftime('%Y%m%d_%H%M%S')))
            write_json(directory/'status.json', dict(timestamp=timestamp(), status='RESUMING'))
        print('Supervisor PID:', detached(directory))
        return 0


if __name__=='__main__':
    sys.exit(main())

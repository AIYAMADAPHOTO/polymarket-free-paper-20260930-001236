"""Foreground container lifecycle; one persistent active experiment, no network listener."""
import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from src.models import utcnow, parse_time
from src.storage import Storage, StorageError, digest, atomic_write
from src.portfolio import validate_state
from src.phase3_audit import write_json, read_json, sources, snapshot, sha, check_frozen
from src.vps_support import environment, ShutdownRequest

TERMINAL={'COMPLETED','INVALID','PREFLIGHT_FAILED','ABORTED_BY_USER_MIGRATION'}


def active_directory(data):
    record=read_json(data/'active.json')
    identity=record['experiment_id']
    if not identity.startswith('phase3_vps_') or '/' in identity or '\\' in identity or '..' in identity:
        raise ValueError('Unsafe/non-VPS active experiment ID')
    result=data/'experiments'/identity
    if not result.is_dir():
        raise ValueError('Active experiment missing; refusing automatic reset')
    return result


def resource_checks(data):
    checks={}
    def check(name,action):
        try:
            value=action()
            if value is False: raise ValueError('check returned false')
            checks[name]=dict(passed=True,evidence=value)
        except Exception as exc:
            checks[name]=dict(passed=False,error=str(exc))
    check('Linux',lambda:platform.system()=='Linux')
    check('Python_3_13_5',lambda:sys.version_info[:3]==(3,13,5))
    check('Docker_container',lambda:Path('/.dockerenv').exists())
    def mount():
        text=Path('/proc/self/mountinfo').read_text()
        # Require the exact persistent mount, not merely a writable container overlay.
        if not any(line.split()[4]==str(data.resolve()) for line in text.splitlines()):
            raise ValueError('Data directory is not a separate persistent mount')
        return str(data.resolve())
    check('persistent_mount',mount)
    def write_probe():
        path=data/'verification'/'atomic_write_probe.json'
        write_json(path,dict(value=1));write_json(path,dict(value=2))
        return read_json(path)=={'value':2}
    check('write_permission_atomic_write',write_probe)
    check('free_disk_1_GiB',lambda:shutil.disk_usage(data).free>=1024**3)
    def memory():
        info={line.split(':')[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if ':' in line and line.split()[1].isdigit()}
        available=info['MemAvailable']
        limit=Path('/sys/fs/cgroup/memory.max')
        current=Path('/sys/fs/cgroup/memory.current')
        if limit.exists() and limit.read_text().strip()!='max':
            available=min(available,int(limit.read_text())-int(current.read_text()))
        else:
            limit=Path('/sys/fs/cgroup/memory/memory.limit_in_bytes')
            current=Path('/sys/fs/cgroup/memory/memory.usage_in_bytes')
            if limit.exists(): available=min(available,int(limit.read_text())-int(current.read_text()))
        if available<192*1024*1024:raise ValueError('Less than 192 MiB available in host/cgroup')
        return dict(available_bytes=available)
    check('available_memory',memory)
    check('DNS_Gamma',lambda:bool(socket.getaddrinfo('gamma-api.polymarket.com',443)))
    check('DNS_CLOB',lambda:bool(socket.getaddrinfo('clob.polymarket.com',443)))
    check('PAPER_fixed_fifty_48h_timezone',environment)
    return dict(timestamp=utcnow().isoformat(),result='PASS' if all(c['passed'] for c in checks.values()) else 'FAIL',checks=checks)


def create_experiment(data, settings):
    if (data/'active.json').exists():
        raise ValueError('Existing experiment pointer; refusing a new account')
    existing=list((data/'experiments').glob('phase3_vps_*'))
    if existing:
        raise ValueError('Orphan experiment exists without pointer; inspect, do not auto-reset')
    identity='phase3_vps_'+utcnow().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]
    directory=data/'experiments'/identity
    (directory/'verification').mkdir(parents=True,exist_ok=False)
    runtime=directory/'runtime';runtime.mkdir()
    for path in sources(ROOT):
        dest=runtime/path.relative_to(ROOT);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,dest)
    shutil.copytree(ROOT/'tests',runtime/'tests',ignore=shutil.ignore_patterns('__pycache__'))
    baseline=ROOT/'baseline.json'
    if not baseline.exists():baseline=ROOT/'verification'/'phase2_read_only_audit.json'
    shutil.copy2(baseline,runtime/'baseline.json')
    for name in ('Dockerfile','docker-compose.yml'):
        shutil.copy2(ROOT/name,runtime/name)
    write_json(directory/'verification'/'phase2_source_baseline.json',read_json(baseline)['source_hashes'])
    frozen=snapshot(runtime);frozen['deployment']=settings
    frozen['runtime']['SCAN_INTERVAL_SECONDS']=settings['SCAN_INTERVAL_SECONDS']
    write_json(directory/'config_snapshot.json',frozen)
    write_json(directory/'manifest.json',dict(experiment_id=identity,created_at=utcnow().isoformat(),
        starting_equity='50.00',start_time=None,scheduled_end_time=None,config_hash=sha(directory/'config_snapshot.json'),
        python=sys.version,duration_seconds=172800,mode='PAPER',deployment='Docker Linux VPS'))
    write_json(directory/'status.json',dict(status='PREPARING',timestamp=utcnow().isoformat()))
    write_json(data/'active.json',dict(experiment_id=identity,created_at=utcnow().isoformat()))
    return directory


def validate_resume(directory, settings):
    manifest=read_json(directory/'manifest.json')
    if not manifest['experiment_id'].startswith('phase3_vps_'):
        raise ValueError('Local experiment must not be reused on VPS')
    if (directory/'migration_disposition.json').exists():raise ValueError('Aborted experiment cannot resume')
    frozen=read_json(directory/'config_snapshot.json')
    if frozen['deployment']!=settings:
        raise ValueError('Deployment settings changed during experiment; resume refused')
    if manifest['python']!=sys.version:
        raise ValueError('Python runtime changed; restore original image')
    check_frozen(directory,directory/'runtime')
    return True


def load_checked_state(directory):
    state=read_json(directory/'state.json')
    checksum=state.pop('checksum')
    if digest(state)!=checksum:raise ValueError('State checksum mismatch')
    validate_state(state)
    return state


def health(data):
    directory=active_directory(data)
    state=load_checked_state(directory)
    status=read_json(directory/'status.json')
    phase=status['status']
    if phase=='COMPLETED':
        final=read_json(directory/'final_report.json')
        if final['status']!='COMPLETED':raise ValueError('Final report not valid')
        return dict(healthy=True,status='COMPLETED',note='idle terminal container; no new experiment')
    if phase!='RUNNING':raise ValueError('worker status '+phase)
    os.kill(int(status['pid']),0)
    telemetry=read_json(directory/'telemetry.json')
    now=utcnow()
    for key,limit in [('last_pulse',60),('last_heartbeat',360),('last_successful_API_request',180)]:
        if not telemetry.get(key):raise ValueError(key+' absent')
        age=(now-parse_time(telemetry[key])).total_seconds()
        if not -5<=age<=limit:raise ValueError(key+' not fresh')
    return dict(healthy=True,status=phase,cash=state['cash_balance'],positions=len(state['positions']))


def show(data):
    directory=active_directory(data)
    result=dict(experiment=str(directory),manifest=read_json(directory/'manifest.json'),status=read_json(directory/'status.json'))
    if (directory/'state.json').exists():
        state=load_checked_state(directory)
        result.update(cash=state['cash_balance'],positions=state['positions'],trades=len(state['trades']),realized_pnl=state['realized_pnl'])
    if (directory/'telemetry.json').exists():result['telemetry']=read_json(directory/'telemetry.json')
    return result


def idle(shutdown):
    # unless-stopped would restart an exited container even after completion or failure.
    # Stay idle, preserving the terminal status; health failures never trigger a restart here.
    while not shutdown.reason:time.sleep(1)


def serve(data):
    shutdown=ShutdownRequest().install()
    try:
        try:
            settings=environment() # Reject non-PAPER before creating any experiment.
        except Exception as exc:
            print('Configuration rejected; no experiment started:',exc,flush=True)
            idle(shutdown)
            return 1
        data.mkdir(parents=True,exist_ok=True)
        with Storage(data/'controller_lock'):
            write_json(data/'controller.json',dict(pid=os.getpid(),timestamp=utcnow().isoformat(),status='starting'))
            try:
                directory=active_directory(data) if (data/'active.json').exists() else None
                if directory:
                    phase=read_json(directory/'status.json')['status']
                    if phase in TERMINAL:
                        print('Terminal experiment retained:',phase,flush=True);idle(shutdown);return 0
                    validate_resume(directory,settings)
                    if (directory/'STOP').exists():
                        print('Explicit experiment STOP remains set; no automatic resume.',flush=True);idle(shutdown);return 0
                resources=resource_checks(data)
                write_json(data/'verification'/('vps_preflight_'+utcnow().strftime('%Y%m%dT%H%M%S_%f')+'.json'),resources)
                if resources['result']!='PASS':raise ValueError('VPS resource preflight failed; inspect persistent verification')
                if directory is None:directory=create_experiment(data,settings)
                if read_json(directory/'manifest.json')['start_time'] is None:
                    preflight=directory/'verification'/'preflight.json'
                    if preflight.exists() and read_json(preflight)['result']=='FAIL':raise ValueError('Prior preflight failure; manual inspection required')
                    child=subprocess.Popen([sys.executable,str(directory/'runtime'/'phase3.py'),'_preflight',str(directory)])
                    while child.poll() is None:
                        if shutdown.reason:
                            child.terminate();child.wait(timeout=60);return 0
                        time.sleep(.2)
                    if child.returncode:
                        write_json(directory/'status.json',dict(status='PREFLIGHT_FAILED',timestamp=utcnow().isoformat()))
                        idle(shutdown);return 1
                failures=0
                while not shutdown.reason:
                    child=subprocess.Popen([sys.executable,'-u',str(directory/'runtime'/'vps.py'),'worker','--data',str(directory)])
                    write_json(data/'controller.json',dict(pid=os.getpid(),worker_pid=child.pid,experiment_id=directory.name,
                                                         status='running',timestamp=utcnow().isoformat()))
                    while child.poll() is None:
                        if shutdown.reason:
                            child.terminate() # SIGTERM: worker handler saves, does not raise during writes.
                            try:child.wait(timeout=60)
                            except subprocess.TimeoutExpired:
                                write_json(data/'controller_shutdown_timeout.json',dict(timestamp=utcnow().isoformat(),pid=child.pid))
                                # Leave final escalation to Docker's declared 90-second grace period.
                                child.wait()
                            return 0
                        time.sleep(.5)
                    phase=read_json(directory/'status.json')['status']
                    if phase in TERMINAL or (directory/'STOP').exists():
                        print('Experiment terminal/explicit stop:',phase,flush=True);idle(shutdown);return 0
                    failures+=1
                    if failures>=3:
                        raise RuntimeError('Three unexpected worker exits; manual inspection required, no restart loop')
                    for _ in range(10):
                        if shutdown.reason:return 0
                        time.sleep(.5)
            except Exception as exc:
                write_json(data/'controller_error.json',dict(timestamp=utcnow().isoformat(),error=str(exc)))
                print('BLOCKED:',exc,flush=True)
                idle(shutdown)
                return 1
    finally:
        shutdown.restore()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['serve','worker','status','health','preflight'])
    parser.add_argument('--data',type=Path,default=Path(os.getenv('BOT_DATA_ROOT','/var/lib/paperbot')))
    args=parser.parse_args()
    if args.command=='serve':return serve(args.data)
    if args.command=='worker':
        settings=environment();validate_resume(args.data,settings)
        os.environ['TZ']=settings['TIMEZONE']
        if hasattr(time,'tzset'):time.tzset()
        from src.phase3_runtime import run_worker
        shutdown=ShutdownRequest().install()
        try:return run_worker(args.data,ROOT,shutdown=shutdown,deployment=settings)
        finally:shutdown.restore()
    if args.command=='preflight':
        result=resource_checks(args.data);print(json.dumps(result,indent=2));return 0 if result['result']=='PASS' else 1
    try:
        print(json.dumps(health(args.data) if args.command=='health' else show(args.data),indent=2,ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps(dict(healthy=False,error=str(exc))));return 1


if __name__=='__main__':sys.exit(main())

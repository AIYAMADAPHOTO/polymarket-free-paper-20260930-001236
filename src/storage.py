"""One-process lock, atomic authoritative state, rebuildable CSV trade ledger."""
import csv
import hashlib
import io
import json
import logging
import os
import tempfile
from pathlib import Path

from .models import timestamp
from .portfolio import initial_state, validate_state, TRADE_FIELDS


class StorageError(RuntimeError):
    pass


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def digest(state):
    return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


class Storage:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'state.json'
        self.log = logging.getLogger('paperbot')
        self.handle = None

    def __enter__(self):
        self.handle = open(self.directory / '.instance.lock', 'a+b')
        if self.handle.tell() == 0:
            self.handle.write(b'0')
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise StorageError('Data directory already in use by another process') from exc
        return self

    def __exit__(self, *args):
        if self.handle:
            self.handle.close()
            self.handle = None

    def require_lock(self):
        if self.handle is None:
            raise StorageError('Storage must be used inside a with block')

    def load(self, balance):
        self.require_lock()
        if not self.path.exists():
            if any((self.directory / name).exists() for name in (
                    'state.json.bak', 'trades.csv', 'portfolio_history.csv', 'market_snapshots.csv')):
                raise StorageError('state.json missing but previous data exists; refusing to reset')
            state = initial_state(balance)
            self.save(state)
            self.log.info('New PAPER account starting_balance=%s', balance)
        else:
            try:
                data = json.loads(self.path.read_text(encoding='utf-8'))
                checksum = data.pop('checksum')
                if checksum != digest(data):
                    raise ValueError('checksum mismatch')
                validate_state(data)
                state = data
            except (ValueError, KeyError, TypeError, OSError, AttributeError) as exc:
                raise StorageError('state.json corrupt/incompatible; original preserved. '
                                   'Inspect state.json.bak; NO automatic reset/rollback. ' + str(exc)) from exc
            self.export_trades(state)
            self.log.info('State restored cash=%s positions=%s trades=%s', state['cash_balance'],
                          len(state['positions']), len(state['trades']))
        self.ensure_csv('portfolio_history.csv', ['timestamp', 'cash_balance', 'realized_pnl',
                        'equity', 'unrealized_pnl', 'valuation_status', 'unpriced_positions'])
        return state

    def save(self, state):
        self.require_lock()
        validate_state(state)
        data = dict(state, checksum=digest(state))
        try:
            if self.path.exists():
                atomic_write(self.directory / 'state.json.bak', self.path.read_text(encoding='utf-8'))
            atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        except OSError as exc:
            raise StorageError('State commit failed; stop and inspect files before restarting') from exc
        self.log.info('State saved trades=%s cash=%s', len(state['trades']), state['cash_balance'])

    def export_trades(self, state):
        self.require_lock()
        output = io.StringIO(newline='')
        writer = csv.DictWriter(output, fieldnames=TRADE_FIELDS)
        writer.writeheader()
        writer.writerows({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                         for k, v in row.items()} for row in state['trades'])
        try:
            atomic_write(self.directory / 'trades.csv', output.getvalue())
        except OSError as exc:
            raise StorageError('Trade CSV export failed; committed state remains authoritative; restart repairs CSV') from exc

    def ensure_csv(self, name, fields):
        path = self.directory / name
        if not path.exists():
            output = io.StringIO(newline='')
            csv.writer(output).writerow(fields)
            atomic_write(self.directory / name, output.getvalue())
        else:
            with path.open(encoding='utf-8', newline='') as stream:
                reader = csv.DictReader(stream)
                if reader.fieldnames == fields:
                    return
                if not reader.fieldnames or not set(reader.fieldnames) <= set(fields):
                    raise StorageError('Incompatible CSV header: ' + name)
                rows = list(reader)
            backup = path.with_name(path.name + '.phase1.bak')
            if not backup.exists():
                atomic_write(backup, path.read_text(encoding='utf-8'))
            output = io.StringIO(newline='')
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
            atomic_write(path, output.getvalue())
            self.log.info('CSV schema expanded without dropping rows: %s', name)

    def append_csv(self, name, row):
        self.require_lock()
        self.ensure_csv(name, list(row))
        with open(self.directory / name, 'a', encoding='utf-8', newline='') as stream:
            csv.DictWriter(stream, fieldnames=list(row)).writerow(row)
            stream.flush()
            os.fsync(stream.fileno())

    def append_snapshot(self, row):
        self.append_csv('market_snapshots.csv', row)

    def append_portfolio(self, state, valuation):
        self.append_csv('portfolio_history.csv', dict(timestamp=timestamp(),
                        cash_balance=state['cash_balance'], realized_pnl=state['realized_pnl'],
                        **{k: json.dumps(v) if isinstance(v, list) else v for k, v in valuation.items()}))

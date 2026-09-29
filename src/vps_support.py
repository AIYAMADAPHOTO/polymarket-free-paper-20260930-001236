"""Deployment-only settings, shutdown flags, and lossless log segmentation."""
import logging
import os
import signal
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo


def environment(environ=None):
    env = os.environ if environ is None else environ
    if env.get('TRADING_MODE','PAPER')!='PAPER':
        raise ValueError('Only TRADING_MODE=PAPER is permitted')
    if Decimal(env.get('STARTING_BALANCE_USD','50.00'))!=Decimal('50.00'):
        raise ValueError('Phase 3 must start with exactly 50.00 fictional USD')
    if Decimal(env.get('PHASE3_DURATION_HOURS','48'))!=48:
        raise ValueError('Phase 3 duration must be exactly 48 hours')
    interval=int(env.get('SCAN_INTERVAL_SECONDS','45'))
    if not 30<=interval<=300:
        raise ValueError('SCAN_INTERVAL_SECONDS must be 30..300, frozen at creation')
    level=env.get('LOG_LEVEL','INFO').upper()
    if level not in ('DEBUG','INFO','WARNING','ERROR'):
        raise ValueError('Invalid LOG_LEVEL')
    zone=env.get('TIMEZONE','Asia/Tokyo')
    ZoneInfo(zone)  # IANA data is installed in the Linux image.
    return dict(TRADING_MODE='PAPER',STARTING_BALANCE_USD='50.00',PHASE3_DURATION_HOURS=48,
                SCAN_INTERVAL_SECONDS=interval,LOG_LEVEL=level,TIMEZONE=zone)


class ShutdownRequest:
    def __init__(self):
        self.reason=None
        self.previous={}

    def receive(self, signum, frame=None):
        # Never raise inside an atomic state commit or a file append.
        self.reason=signal.Signals(signum).name

    def install(self):
        for sig in (signal.SIGTERM,signal.SIGINT):
            self.previous[sig]=signal.signal(sig,self.receive)
        return self

    def restore(self):
        for sig,previous in self.previous.items():
            signal.signal(sig,previous)


class ArchiveLogHandler(logging.FileHandler):
    """Bound active file size without deleting audit history. Disk budget is checked separately."""
    def __init__(self, filename, max_bytes=10*1024*1024):
        self.max_bytes=max_bytes
        super().__init__(filename,encoding='utf-8')

    def emit(self, record):
        if self.stream and self.stream.tell()>=self.max_bytes:
            self.stream.close()
            source=Path(self.baseFilename)
            stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%f')
            os.replace(source,source.with_name(f'runtime.{stamp}.{os.getpid()}.log'))
            self.stream=self._open()
        super().emit(record)

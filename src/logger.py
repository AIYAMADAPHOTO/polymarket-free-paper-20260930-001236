import logging
from logging.handlers import RotatingFileHandler
import time


def setup_logger(directory):
    directory.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger('paperbot')
    log.setLevel(logging.INFO)
    log.handlers.clear()
    formatter = logging.Formatter('%(asctime)sZ %(levelname)s %(message)s')
    formatter.converter = time.gmtime
    for handler in (logging.StreamHandler(), RotatingFileHandler(
            directory / 'bot.log', maxBytes=5_000_000, backupCount=3, encoding='utf-8')):
        handler.setFormatter(formatter)
        log.addHandler(handler)
    return log

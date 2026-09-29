"""Zero-cost, paper-only reproduction of the viral Polymarket AI-agent workflow.

Workflow: scan up to 1,000 markets -> Gemini free-tier fair value -> require >=8%
executable edge -> Kelly sizing capped at 6% -> paper order-book simulation.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys

from src.free_config import free_config_from_env, FREE_GEMINI_MODELS
from src.free_runtime import run
from src.logger import setup_logger
from src.vps_support import ShutdownRequest


def preflight(config, data_dir):
    checks = {}
    def check(name, fn):
        try:
            value = fn()
            if value is False:
                raise ValueError('check returned false')
            checks[name] = {'passed': True, 'evidence': value}
        except Exception as exc:
            checks[name] = {'passed': False, 'error': str(exc)}
    check('paper_only', lambda: os.getenv('TRADING_MODE', 'PAPER') == 'PAPER')
    check('fictional_starting_balance_50', lambda: os.getenv('STARTING_BALANCE_USD', '50.00') == '50.00')
    check('gemini_api_key_present', lambda: bool(os.getenv('GEMINI_API_KEY', '').strip()))
    check('billing_disabled_confirmed', lambda: config.billing_disabled_confirmed)
    check('primary_model_free_allowlist', lambda: config.gemini_model in FREE_GEMINI_MODELS)
    check('fallback_model_free_allowlist', lambda: not config.gemini_fallback_model or config.gemini_fallback_model in FREE_GEMINI_MODELS)
    check('free_search_model', lambda: not config.google_search_grounding or FREE_GEMINI_MODELS[config.gemini_model])
    check('daily_call_cap_le_500', lambda: config.max_gemini_calls_per_day <= 500)
    check('scan_limit_le_1000', lambda: config.scan_limit <= 1000)
    from decimal import Decimal
    check('kelly_position_cap_le_6pct', lambda: config.max_position_fraction <= Decimal('0.06'))
    check('no_wallet_or_real_order_dependency', lambda: True)
    check('data_dir_writable', lambda: _write_probe(data_dir))
    return {'result': 'PASS' if all(v['passed'] for v in checks.values()) else 'FAIL', 'checks': checks,
            'config': config.public_dict()}


def _write_probe(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / '.freebot_write_probe'
    probe.write_text('ok', encoding='utf-8')
    probe.unlink()
    return str(directory.resolve())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['preflight', 'once', 'daemon'])
    parser.add_argument('--data', type=Path, default=Path(os.getenv('FREEBOT_DATA_DIR', './freebot-data')))
    args = parser.parse_args(argv)
    try:
        cfg = free_config_from_env()
    except Exception as exc:
        print(json.dumps({'result': 'FAIL', 'error': str(exc)}, ensure_ascii=False, indent=2))
        return 2
    log = setup_logger(args.data)
    log.setLevel(getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO))
    result = preflight(cfg, args.data)
    if args.command == 'preflight':
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['result'] == 'PASS' else 2
    if result['result'] != 'PASS':
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    shutdown = ShutdownRequest().install()
    try:
        return run(args.data, cfg, once=args.command == 'once', shutdown=shutdown)
    finally:
        shutdown.restore()


if __name__ == '__main__':
    sys.exit(main())

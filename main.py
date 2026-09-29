"""Windows-friendly entrypoint. Public GET + local paper transactions only."""
import argparse
import json
import os
import sys
import time
from decimal import Decimal

from config import ROOT, SCAN_INTERVAL_SECONDS, MAX_MARKETS
from config import validate_trading_mode
from src.logger import setup_logger
from src.models import decimal, timestamp
from src.polymarket_client import PolymarketClient, ApiError
from src.market_scanner import MarketScanner
from src.paper_broker import PaperBroker
from src.storage import Storage, StorageError, atomic_write


def run_test(broker, scanner, markets, leg):
    checkpoint = broker.state['test_run']
    if checkpoint and checkpoint['stage'] == 'completed':
        return 'already_completed_no_duplicate_trades'
    if not checkpoint:
        if leg == 'sell':
            raise ValueError('No pending TEST_MODE buy')
        if broker.get_positions():
            raise ValueError('TEST_MODE starts only with no existing positions; use separate --data-dir')
        for candidate in markets:
            try:
                market = scanner.fetch_market(candidate.market_id)
                quote = market.quotes.get('YES')
                if not quote or quote.bid is None or quote.ask is None:
                    continue
                quantity = Decimal('1')
                if quote.bid_size < quantity or quote.ask_size < quantity:
                    continue
                checkpoint = dict(stage='bought', market_id=market.market_id, outcome='YES', quantity='1')
                broker.buy(market, 'YES', quantity, reason='TEST_MODE one-share BUY', test_checkpoint=checkpoint)
                break
            except (ValueError, ApiError) as exc:
                scanner.log.warning('TEST_MODE candidate rejected %s: %s', candidate.market_id, exc)
        else:
            raise ValueError('No fresh, fee-known, two-sided market for TEST_MODE')
    if leg == 'buy':
        return 'bought_pending_sell'
    # Always re-read metadata and bid; never sell against a cached buy quote.
    market = scanner.fetch_market(checkpoint['market_id'])
    broker.sell(market, checkpoint['outcome'], checkpoint['quantity'],
                reason='TEST_MODE one-share SELL', test_checkpoint=dict(checkpoint, stage='completed'))
    return 'completed'


def run_cycle(args, broker, scanner):
    test_result = 'disabled'
    pending = broker.state['test_run']
    if args.buy or args.sell or args.close:
        market = scanner.fetch_market(args.buy or args.sell or args.close)
        if args.buy:
            broker.buy(market, args.outcome, args.quantity, args.price)
        elif args.sell:
            broker.sell(market, args.outcome, args.quantity, args.price)
        else:
            broker.close_position(market, args.outcome)
        markets = [market]
    elif args.test_mode and pending and pending['stage'] == 'bought':
        markets = []
        test_result = run_test(broker, scanner, markets, args.test_leg)
        markets = list(broker.markets.values())
    else:
        markets = scanner.scan()
        broker.mark(markets)
        if args.test_mode:
            test_result = run_test(broker, scanner, markets, args.test_leg)
            markets = list(broker.markets.values())
    # Positions outside the discovery window must also be priced on every cycle.
    by_id = {m.market_id: m for m in markets}
    for position in broker.get_positions().values():
        mid = position['market_id']
        if mid not in by_id:
            try:
                by_id[mid] = scanner.fetch_market(mid)
            except (ApiError, ValueError, KeyError, TypeError) as exc:
                scanner.log.warning('Position valuation unavailable market=%s: %s', mid, exc)
    broker.mark(by_id.values())
    broker.record_portfolio()
    return dict(timestamp=timestamp(), mode='PAPER_ONLY', status='ok', scan=scanner.stats,
                test_mode=args.test_mode, test_result=test_result,
                cash_balance=str(broker.get_cash_balance()), realized_pnl=str(broker.get_realized_pnl()),
                positions=broker.get_positions(), trade_count=len(broker.state['trades']), **broker.valuation())


def parser():
    p = argparse.ArgumentParser(description='READ ONLY market API + local PAPER trading (no money transfer)')
    p.add_argument('--data-dir', type=__import__('pathlib').Path, default=ROOT / 'data')
    p.add_argument('--once', action='store_true', help='one cycle then exit')
    p.add_argument('--cycles', type=int, default=0, help='0 = keep scanning until Ctrl+C')
    p.add_argument('--interval', type=float, default=SCAN_INTERVAL_SECONDS)
    p.add_argument('--max-markets', type=int, default=MAX_MARKETS)
    p.add_argument('--strategy', action='store_true', help='Phase 2 automatic PAPER strategy engine')
    p.add_argument('--run-seconds', type=float, default=0, help='bounded runtime (strategy mode only)')
    p.add_argument('--test-mode', action='store_true', default=os.getenv('TEST_MODE', 'false').lower() == 'true')
    p.add_argument('--test-leg', choices=['roundtrip', 'buy', 'sell'], default='roundtrip')
    commands = p.add_mutually_exclusive_group()
    commands.add_argument('--status', action='store_true', help='restore and print saved state without API')
    commands.add_argument('--buy', metavar='MARKET_ID', help='manual PAPER buy')
    commands.add_argument('--sell', metavar='MARKET_ID', help='manual PAPER sell')
    commands.add_argument('--close', metavar='MARKET_ID', help='close PAPER position')
    p.add_argument('--outcome', choices=['YES', 'NO'], default='YES')
    p.add_argument('--quantity', type=decimal)
    p.add_argument('--price', type=decimal, help='optional paper limit; otherwise current best quote')
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        validate_trading_mode()
    except ValueError as exc:
        p.error(str(exc))
    if args.strategy and (args.test_mode or args.buy or args.sell or args.close or args.status):
        p.error('--strategy cannot be combined with TEST_MODE/manual/status')
    if not 0 <= args.run_seconds <= 172800 or (args.run_seconds and not args.strategy):
        p.error('invalid --run-seconds (strategy only, maximum 48h)')
    if args.max_markets < 1 or args.max_markets > 1000 or args.cycles < 0 or not 0.1 <= args.interval <= 3600:
        p.error('invalid market/cycle/interval settings')
    if (args.buy or args.sell) and args.quantity is None:
        p.error('--quantity required for manual BUY/SELL')
    if args.test_mode and (args.buy or args.sell or args.close):
        p.error('manual actions and TEST_MODE cannot be combined')
    if args.test_leg != 'roundtrip' and not args.test_mode:
        p.error('--test-leg requires TEST_MODE=true or --test-mode')
    log = setup_logger(args.data_dir.resolve().parent / ('logs' if args.data_dir.name == 'data' else args.data_dir.name + '-logs'))
    log.info('Startup PAPER_ONLY TEST_MODE=%s data=%s', args.test_mode, args.data_dir.resolve())
    try:
        with Storage(args.data_dir) as storage:
            from src.execution import DepthFill
            broker = PaperBroker(storage, DepthFill() if args.strategy else None)
            if args.status:
                print(json.dumps(broker.state, ensure_ascii=True, indent=2))
                return 0
            scanner = MarketScanner(PolymarketClient(), storage, args.max_markets)
            if args.strategy:
                from src.phase2_runtime import run_strategy
                return run_strategy(args, broker, scanner, storage)
            failures = cycle = 0
            while True:
                cycle += 1
                try:
                    result = run_cycle(args, broker, scanner)
                    failures = 0
                except (ApiError, ValueError, KeyError, TypeError) as exc:
                    log.exception('Cycle exception; state retained; next cycle will retry')
                    failures += 1
                    broker.mark([])
                    broker.record_portfolio()
                    result = dict(timestamp=timestamp(), status='error', error=str(exc),
                                  cash_balance=str(broker.get_cash_balance()), trade_count=len(broker.state['trades']))
                atomic_write(storage.directory / 'last_run.json', json.dumps(result, ensure_ascii=False, indent=2) + '\n')
                print(json.dumps(result, ensure_ascii=True))
                if args.once or args.buy or args.sell or args.close or (args.cycles and cycle >= args.cycles):
                    return 0 if result['status'] == 'ok' else 1
                time.sleep(min(300, args.interval * 2 ** min(failures, 3)))
    except KeyboardInterrupt:
        log.info('Ctrl+C received; committed state preserved')
        return 0
    except (StorageError, OSError) as exc:
        log.exception('Fatal storage exception; stopped without resetting state: %s', exc)
        return 2
    finally:
        log.info('Shutdown PAPER_ONLY')


if __name__ == '__main__':
    sys.exit(main())

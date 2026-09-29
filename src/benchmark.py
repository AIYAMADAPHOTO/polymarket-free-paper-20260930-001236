"""Fixed-budget deterministic buy-and-hold, independent of strategy signals."""
from decimal import Decimal, ROUND_DOWN
from .execution import DepthFill


def start_benchmark(starting_equity, now):
    return dict(start_time=now.isoformat(), starting_equity=str(starting_equity), cash=str(starting_equity),
                position=None, attempted=False, status='pending_first_eligible_market', fees='0', slippage_cost='0')


def update_benchmark(benchmark, eligible, markets, config):
    model = DepthFill()
    if not benchmark['attempted'] and eligible:
        # Predeclared selection rule, unrelated to winning trades or later price movement.
        market, outcome = sorted(eligible, key=lambda pair: (int(pair[0].market_id), pair[1]))[0]
        quote = market.quotes[outcome]
        budget = min(config.benchmark_budget_usd, Decimal(benchmark['cash']))
        qty = (budget / (quote.ask * Decimal('1.1'))).quantize(Decimal('.000001'), rounding=ROUND_DOWN)
        benchmark['attempted'] = True
        try:
            fill = model.execute('BUY', qty, quote, quote.ask * (1 + config.max_entry_slippage_fraction))
            fee = fill.fee(market.fee)
            if fill.notional + fee > budget:
                raise ValueError('benchmark fee exceeds budget')
            benchmark.update(cash=str(Decimal(benchmark['cash']) - fill.notional - fee), fees=str(fee),
                             slippage_cost=str(fill.slippage_cost), status='holding',
                             position=dict(market_id=market.market_id, outcome=outcome, quantity=str(qty),
                                           entry_price=str(fill.price), entry_time=quote.fetched_at,
                                           fill_legs=[[str(p), str(q)] for p, q in fill.legs]))
        except ValueError as exc:
            benchmark['status'] = 'entry_rejected: ' + str(exc)
    equity = Decimal(benchmark['cash'])
    if benchmark['position']:
        p = benchmark['position']
        try:
            market = markets[p['market_id']]
            q = market.quotes[p['outcome']]
            q.require_fresh(config.max_data_age_seconds)
            fill = model.execute('SELL', Decimal(p['quantity']), q)
            equity += fill.notional - fill.fee(market.fee)
        except (KeyError, ValueError):
            equity = None
    benchmark['current_equity'] = str(equity) if equity is not None else None
    benchmark['net_pnl'] = str(equity - Decimal(benchmark['starting_equity'])) if equity is not None else None
    return benchmark

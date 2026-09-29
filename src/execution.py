"""Depth consumption at observed prices only. No network or order submission."""
from dataclasses import dataclass
from decimal import Decimal, localcontext, ROUND_HALF_UP


@dataclass(frozen=True)
class FillResult:
    requested_quantity: Decimal
    quantity: Decimal
    price: Decimal
    notional: Decimal
    slippage: Decimal
    slippage_cost: Decimal
    legs: tuple
    status: str

    def fee(self, schedule):
        return sum((schedule.calculate(q, p) for p, q in self.legs), Decimal('0'))


class DepthFill:
    def execute(self, side, quantity, quote, limit=None, allow_partial=False):
        with localcontext() as ctx:
            ctx.prec = 60
            if quantity <= 0 or not quantity.is_finite() or side not in ('BUY', 'SELL'):
                raise ValueError('invalid depth request')
            levels = quote.asks if side == 'BUY' else quote.bids
            best = quote.ask if side == 'BUY' else quote.bid
            if levels is None:
                raise ValueError('multi-level depth unavailable')
            if not levels or best is None:
                raise ValueError('insufficient liquidity: empty book')
            ordered = sorted(levels, reverse=side == 'SELL')
            if ordered[0][0] != best:
                raise ValueError('depth and best price mismatch')
            remaining, legs = quantity, []
            for price, available in ordered:
                if not 0 < price < 1 or available <= 0:
                    raise ValueError('invalid depth level')
                if limit is not None and ((side == 'BUY' and price > limit) or (side == 'SELL' and price < limit)):
                    break
                taken = min(remaining, available)
                if taken > 0:
                    legs.append((price, taken))
                    remaining -= taken
                if remaining == 0:
                    break
            filled = quantity - remaining
            if filled == 0 or (remaining > 0 and not allow_partial):
                raise ValueError('insufficient liquidity/limit: full fill rejected')
            notional = sum((p * q for p, q in legs), Decimal('0'))
            avg = (notional / filled).quantize(Decimal('0.000000000000000001'), rounding=ROUND_HALF_UP)
            slip_cost = notional - filled * best if side == 'BUY' else filled * best - notional
            return FillResult(quantity, filled, avg, notional, slip_cost / filled,
                              slip_cost, tuple(legs), 'partial' if remaining else 'filled')


def liquidation(broker, markets):
    """Executable liquidation value after modeled exit fees; unknown stays null."""
    total = broker.get_cash_balance()
    missing = []
    model = DepthFill()
    for key, position in broker.get_positions().items():
        try:
            market = markets[position['market_id']]
            q = market.quotes[position['outcome']]
            from config import MAX_QUOTE_AGE_SECONDS
            q.require_fresh(MAX_QUOTE_AGE_SECONDS)
            result = model.execute('SELL', Decimal(position['quantity']), q)
            total += result.notional - result.fee(market.fee)
        except (KeyError, ValueError):
            missing.append(key)
    return (None if missing else total), missing

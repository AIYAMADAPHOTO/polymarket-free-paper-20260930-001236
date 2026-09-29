from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from ..execution import DepthFill


@dataclass(frozen=True)
class SizingResult:
    quantity: Decimal
    budget: Decimal
    cost: Decimal
    reason: str


class PositionSizer:
    def __init__(self, config):
        self.config = config
        self.model = DepthFill()

    def size(self, state, equity, market, outcome):
        c = self.config
        if equity is None or equity <= 0:
            return SizingResult(Decimal('0'), Decimal('0'), Decimal('0'), 'equity_unavailable')
        exposure = sum((Decimal(p['cost_basis']) for p in state['positions'].values()), Decimal('0'))
        existing = sum((Decimal(p['cost_basis']) for p in state['positions'].values()
                        if p['market_id'] == market.market_id), Decimal('0'))
        budget = min(equity * c.risk_fraction, c.max_market_usd - existing,
                     equity * c.max_exposure_fraction - exposure, Decimal(state['cash_balance']))
        if budget < c.min_entry_usd:
            return SizingResult(Decimal('0'), budget, Decimal('0'), 'budget_below_minimum')
        quote = market.quotes[outcome]
        if quote.ask is None:
            return SizingResult(Decimal('0'), budget, Decimal('0'), 'ask_missing')
        limit = min(Decimal('.999999'), quote.ask * (1 + c.max_entry_slippage_fraction))
        step = Decimal('.000001')
        lo, hi, best_cost = 0, int((budget / quote.ask / step).to_integral_value(rounding=ROUND_DOWN)), Decimal('0')
        while lo < hi:
            units = (lo + hi + 1) // 2
            qty = units * step
            try:
                fill = self.model.execute('BUY', qty, quote, limit)
                cost = fill.notional + fill.fee(market.fee)
                valid = cost <= budget
            except ValueError:
                valid = False
            if valid:
                lo, best_cost = units, cost
            else:
                hi = units - 1
        if lo == 0 or best_cost < c.min_entry_usd:
            return SizingResult(Decimal('0'), budget, Decimal('0'), 'insufficient_usable_depth')
        fill = self.model.execute('BUY', lo * step, quote, limit)
        return SizingResult(lo * step, budget, fill.notional + fill.fee(market.fee), 'worst_case_loss_capped')

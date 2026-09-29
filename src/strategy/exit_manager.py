from decimal import Decimal
from ..models import parse_time
from ..execution import DepthFill


class ExitManager:
    def __init__(self, config):
        self.config = config

    def evaluate(self, position, metadata, market, now, opposite_score=Decimal('0')):
        c, reasons = self.config, []
        if market is None:
            return ['market_data_unavailable'], None
        age = (now - parse_time(metadata['entry_time'])).total_seconds()
        if age >= c.time_stop_seconds:
            reasons.append('time_stop')
        if market.end_time and (parse_time(market.end_time) - now).total_seconds() <= c.resolution_exit_seconds:
            reasons.append('market_nearing_resolution')
        if market.liquidity is None or market.liquidity < Decimal(metadata['entry_liquidity']) * c.liquidity_exit_ratio:
            reasons.append('liquidity_disappearance')
        quote = market.quotes.get(position['outcome'])
        if quote is None:
            return reasons + ['book_unavailable'], None
        if quote.spread is None or quote.spread > c.abnormal_spread:
            reasons.append('abnormal_spread')
        if opposite_score >= c.min_combined_score:
            reasons.append('signal_reversal')
        net = None
        try:
            quote.require_fresh(c.max_data_age_seconds)
            fill = DepthFill().execute('SELL', Decimal(position['quantity']), quote)
            cost = Decimal(position['cost_basis'])
            net = fill.notional - fill.fee(market.fee) - cost
            ratio = net / cost if cost else Decimal('0')
            if ratio >= c.take_profit_fraction:
                reasons.append('take_profit')
            if ratio <= -c.stop_loss_fraction:
                reasons.append('stop_loss')
            if net <= -Decimal(metadata['risk_budget']):
                reasons.append('max_loss_per_trade')
        except ValueError:
            reasons.append('executable_exit_unavailable')
        return reasons, net

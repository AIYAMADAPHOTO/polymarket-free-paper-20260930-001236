from dataclasses import dataclass
from decimal import Decimal
from ..models import parse_time


def nearby_depth(quote, config):
    if quote.bids is None or quote.asks is None:
        return None
    if quote.bid is None or quote.ask is None:
        return None
    bids = [(p, q) for p, q in quote.bids if p >= quote.bid - config.depth_price_band][:config.depth_levels]
    asks = [(p, q) for p, q in quote.asks if p <= quote.ask + config.depth_price_band][:config.depth_levels]
    b = sum((q for _, q in bids), Decimal('0'))
    a = sum((q for _, q in asks), Decimal('0'))
    return dict(bid_quantity=b, ask_quantity=a,
                bid_usd=sum((p * q for p, q in bids), Decimal('0')),
                ask_usd=sum((p * q for p, q in asks), Decimal('0')),
                imbalance=(b - a) / (b + a) if b + a else None,
                bid_levels=len(bids), ask_levels=len(asks))


@dataclass(frozen=True)
class FilterResult:
    eligible: bool
    reasons: tuple
    features: dict


class MarketFilter:
    def __init__(self, config):
        self.config = config

    def evaluate(self, market, outcome, now):
        c, reasons, features = self.config, [], {}
        if market.status != 'active_accepting_orders':
            reasons.append('inactive_or_closed')
        age = (now - parse_time(market.fetched_at)).total_seconds()
        if age < 0 or age > c.max_data_age_seconds:
            reasons.append('stale_or_future_metadata')
        if market.end_time is None:
            reasons.append('end_time_missing')
        else:
            remaining = (parse_time(market.end_time) - now).total_seconds()
            features['remaining_seconds'] = str(remaining)
            if remaining < c.min_remaining_seconds:
                reasons.append('near_resolution_or_ended')
        for field, minimum in (('volume', c.min_volume), ('liquidity', c.min_liquidity)):
            value = getattr(market, field)
            features[field] = str(value) if value is not None else None
            if value is None:
                reasons.append(field + '_missing')
            elif value < minimum:
                reasons.append(field + '_too_low')
        if market.fee.status == 'unknown':
            reasons.append('fee_status_unknown')
        quote = market.quotes.get(outcome)
        if quote is None or quote.bid is None or quote.ask is None:
            return FilterResult(False, tuple(reasons + ['bid_ask_missing']), features)
        if parse_time(quote.fetched_at) > now:
            reasons.append('future_quote')
        try:
            quote.require_fresh(c.max_data_age_seconds)
        except ValueError:
            reasons.append('stale_or_invalid_book')
        if not 0 < quote.bid <= quote.ask < 1:
            return FilterResult(False, tuple(reasons + ['abnormal_price']), features)
        if not c.min_price <= quote.bid <= quote.ask <= c.max_price:
            reasons.append('price_outside_entry_range')
        spread = quote.spread
        features['spread'] = str(spread)
        if spread > c.max_spread or spread / quote.ask > c.max_relative_spread:
            reasons.append('spread_too_wide')
        depth = nearby_depth(quote, c)
        if depth is None:
            reasons.append('depth_unavailable')
        else:
            features.update({k: str(v) if v is not None else None for k, v in depth.items()})
            if depth['bid_usd'] < c.min_depth_usd or depth['ask_usd'] < c.min_depth_usd:
                reasons.append('insufficient_nearby_depth')
        return FilterResult(not reasons, tuple(reasons), features)

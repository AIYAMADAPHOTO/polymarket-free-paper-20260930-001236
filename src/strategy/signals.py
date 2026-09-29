from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json

from ..models import parse_time
from .market_filter import nearby_depth

ZERO, ONE = Decimal('0'), Decimal('1')


@dataclass(frozen=True)
class Signal:
    strategy: str
    score: Decimal
    reason: str
    features: dict


def observation(market, outcome, config):
    q = market.quotes.get(outcome)
    if q is None or q.bid is None or q.ask is None:
        return None
    depth = nearby_depth(q, config)
    # Availability timestamp is the LAST component arrival, never the earlier Gamma time.
    available = max(parse_time(q.fetched_at), parse_time(market.fetched_at)).isoformat()
    return dict(timestamp=available, market_id=market.market_id, outcome=outcome,
                bid=str(q.bid), ask=str(q.ask), volume=str(market.volume) if market.volume is not None else None,
                liquidity=str(market.liquidity) if market.liquidity is not None else None,
                spread=str(q.spread), imbalance=str(depth['imbalance']) if depth and depth['imbalance'] is not None else None,
                bid_levels=depth['bid_levels'] if depth else 0, ask_levels=depth['ask_levels'] if depth else 0,
                book_timestamp=q.book_timestamp)


def causal_window(history, as_of, config):
    rows = sorted((r for r in history if 0 <= (as_of - parse_time(r['timestamp'])).total_seconds() <= config.history_seconds),
                  key=lambda r: r['timestamp'])
    unique = {r['timestamp']: r for r in rows}
    rows = list(unique.values())
    if len(rows) < config.history_min_points:
        return []
    if (parse_time(rows[-1]['timestamp']) - parse_time(rows[0]['timestamp'])).total_seconds() < config.history_min_span_seconds:
        return []
    if any((parse_time(b['timestamp']) - parse_time(a['timestamp'])).total_seconds() > config.history_max_gap_seconds
           for a, b in zip(rows, rows[1:])):
        return []
    return rows


def momentum(rows, config):
    if not rows:
        return Signal('momentum', ZERO, 'history_warmup_or_gap', {})
    prices = [Decimal(r['bid']) for r in rows]
    move = prices[-1] - prices[0]
    features = {'momentum': str(move)}
    if any(r['volume'] is None or r['liquidity'] is None for r in rows):
        return Signal('momentum', ZERO, 'volume_liquidity_unavailable', features)
    volume0, volume1 = Decimal(rows[0]['volume']), Decimal(rows[-1]['volume'])
    liq0, liq1 = Decimal(rows[0]['liquidity']), Decimal(rows[-1]['liquidity'])
    growth = (volume1 - volume0) / volume0 if volume0 > 0 else ZERO
    ratio = liq1 / liq0 if liq0 > 0 else ZERO
    steps = [b - a for a, b in zip(prices, prices[1:])]
    positive = sum(1 for s in steps if s > 0)
    features.update(volume_growth=str(growth), liquidity_ratio=str(ratio), positive_steps=positive)
    if (move < config.momentum_min_move or positive < 3 or positive * 2 < len(steps)
            or growth < config.momentum_volume_growth or ratio < config.momentum_liquidity_ratio):
        return Signal('momentum', ZERO, 'move_flow_or_continuity_below_threshold', features)
    score = min(ONE, Decimal('.7') + Decimal('.3') * (move / config.momentum_min_move - 1))
    return Signal('momentum', score, 'sustained_bid_rise_with_volume_and_liquidity', features)


def mean_reversion(rows, config):
    if not rows:
        return Signal('mean_reversion', ZERO, 'history_warmup_or_gap', {})
    prices = [Decimal(r['bid']) for r in rows]
    baseline = sum(prices[:-3], ZERO) / len(prices[:-3]) if len(prices) > 3 else prices[0]
    low = min(prices[-3:])
    shock, rebound = baseline - low, prices[-1] - low
    features = {'shock': str(shock), 'rebound': str(rebound), 'baseline': str(baseline)}
    # Need two observed rising samples after a dip; never catch an ongoing falling knife.
    if (shock < config.reversion_min_shock or rebound < config.reversion_confirm_move
            or not prices[-3] < prices[-2] < prices[-1] or prices[-1] >= baseline):
        return Signal('mean_reversion', ZERO, 'no_confirmed_rebound_after_shock', features)
    if any(r['liquidity'] is None for r in rows[-3:]):
        return Signal('mean_reversion', ZERO, 'liquidity_unavailable', features)
    if Decimal(rows[-1]['liquidity']) < Decimal(rows[-3]['liquidity']) * config.momentum_liquidity_ratio:
        return Signal('mean_reversion', ZERO, 'liquidity_still_disappearing', features)
    score = min(ONE, Decimal('.7') + Decimal('.3') * (shock / config.reversion_min_shock - 1))
    return Signal('mean_reversion', score, 'shock_then_two_step_rebound', features)


def imbalance(rows, config):
    if not rows:
        return Signal('order_book_imbalance', ZERO, 'history_warmup_or_gap', {})
    end = parse_time(rows[-1]['timestamp'])
    recent = [r for r in rows if (end - parse_time(r['timestamp'])).total_seconds() <= config.imbalance_persistence_seconds + config.history_max_gap_seconds]
    if (end - parse_time(recent[0]['timestamp'])).total_seconds() < config.imbalance_persistence_seconds:
        return Signal('order_book_imbalance', ZERO, 'persistence_too_short', {})
    if any(r['imbalance'] is None or r['bid_levels'] < 2 or r['ask_levels'] < 2 for r in recent):
        return Signal('order_book_imbalance', ZERO, 'multi_level_depth_unavailable', {})
    values = [Decimal(r['imbalance']) for r in recent]
    weakest = min(values)
    features = {'order_book_imbalance': str(values[-1]), 'weakest_imbalance': str(weakest)}
    if weakest < config.imbalance_threshold:
        return Signal('order_book_imbalance', ZERO, 'imbalance_not_sustained', features)
    # Repeated identical book payloads are not independent evidence of persistent flow.
    if len({r['book_timestamp'] for r in recent}) < 3:
        return Signal('order_book_imbalance', ZERO, 'unchanged_book_not_confirmed', features)
    score = min(ONE, Decimal('.7') + Decimal('.3') * (weakest - config.imbalance_threshold) / (1 - config.imbalance_threshold))
    return Signal('order_book_imbalance', score, 'persistent_near_touch_depth_bias', features)


def generate(history, as_of, config):
    rows = causal_window(history, as_of, config)
    return [momentum(rows, config), mean_reversion(rows, config), imbalance(rows, config)]


def combine(signals, config):
    weights = {'momentum': config.weight_momentum, 'mean_reversion': config.weight_mean_reversion,
               'order_book_imbalance': config.weight_imbalance}
    active = [s for s in signals if s.score >= config.min_signal_score and weights[s.strategy] > 0]
    if not active:
        return ZERO, []
    score = sum((s.score * weights[s.strategy] for s in active), ZERO) / sum((weights[s.strategy] for s in active), ZERO)
    # Agreement bonus, capped. Inactive strategies do not dilute independent signals.
    return min(ONE, score + Decimal('.05') * (len(active) - 1)), active


def signal_id(market_id, outcome, active, last_observation):
    content = [market_id, outcome, [(s.strategy, str(s.score)) for s in active], last_observation]
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()

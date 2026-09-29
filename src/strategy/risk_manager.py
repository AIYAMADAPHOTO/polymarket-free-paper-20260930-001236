from dataclasses import dataclass
from decimal import Decimal
from ..models import parse_time


def completed_positions(trades):
    pending, closed = {}, []
    for trade in trades:
        key = trade['market_id'] + ':' + trade['outcome']
        if trade['side'] == 'BUY':
            pending.setdefault(key, dict(net=Decimal('0'), strategy=trade.get('strategy') or 'phase1/manual',
                                         entry_time=trade['timestamp'], entry_cost=Decimal('0')))
            pending[key]['entry_cost'] += Decimal(trade['cash_before']) - Decimal(trade['cash_after'])
        elif key in pending:
            pending[key]['net'] += Decimal(trade['realized_pnl'])
            if Decimal(trade['position_after']) == 0:
                record = pending.pop(key)
                record.update(market_id=trade['market_id'], timestamp=trade['timestamp'],
                              holding_seconds=Decimal(str((parse_time(trade['timestamp']) - parse_time(record['entry_time'])).total_seconds())))
                closed.append(record)
    return closed


@dataclass(frozen=True)
class RiskResult:
    allowed: bool
    reasons: tuple


class RiskManager:
    def __init__(self, config):
        self.config = config

    def check(self, state, engine, market_id, decision_id, equity, now):
        c, reasons = self.config, []
        positions = state['positions']
        if equity is None or equity <= 0:
            reasons.append('equity_unavailable_or_nonpositive')
        if any(p['market_id'] == market_id for p in positions.values()):
            reasons.append('duplicate_position_no_averaging_down')
        if len(positions) >= c.max_positions:
            reasons.append('max_simultaneous_positions')
        exposure = sum((Decimal(p['cost_basis']) for p in positions.values()), Decimal('0'))
        if equity is not None and exposure >= equity * c.max_exposure_fraction:
            reasons.append('max_portfolio_exposure')
        if decision_id in engine['processed_signals']:
            reasons.append('duplicate_signal')
        today = now.date().isoformat()
        daily_realized = sum((Decimal(t['realized_pnl']) for t in state['trades']
                              if parse_time(t['timestamp']).date().isoformat() == today), Decimal('0'))
        daily = engine['daily']
        if -daily_realized >= c.daily_loss_limit_usd:
            reasons.append('daily_realized_loss_limit')
        if equity is not None and daily.get('date') == today and Decimal(daily['starting_equity']) - equity >= c.daily_loss_limit_usd:
            reasons.append('daily_equity_loss_limit')
        closed = completed_positions(state['trades'])
        streak = 0
        for trade in reversed(closed):
            if trade['net'] >= 0:
                break
            streak += 1
        if streak >= c.consecutive_loss_limit and (now - parse_time(closed[-1]['timestamp'])).total_seconds() < c.consecutive_halt_seconds:
            reasons.append('consecutive_loss_halt')
        same_market = [t for t in state['trades'] if t['market_id'] == market_id]
        if same_market:
            last = same_market[-1]
            cooldown = c.cooldown_after_loss_seconds if Decimal(last['realized_pnl']) < 0 else c.cooldown_seconds
            if (now - parse_time(last['timestamp'])).total_seconds() < cooldown:
                reasons.append('market_cooldown_after_loss' if Decimal(last['realized_pnl']) < 0 else 'market_cooldown')
        losing = [t for t in closed if t['net'] < 0]
        if losing and (now - parse_time(losing[-1]['timestamp'])).total_seconds() < c.cooldown_after_loss_seconds:
            reasons.append('portfolio_cooldown_after_loss')
        return RiskResult(not reasons, tuple(reasons))

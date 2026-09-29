"""Descriptive metrics, including zero-trade and unknown-valuation cases."""
from decimal import Decimal
from .strategy.risk_manager import completed_positions

ZERO = Decimal('0')


def drawdown(curve):
    peak = None
    worst, percent = ZERO, ZERO
    for value in curve:
        if value is None:
            continue
        value = Decimal(value)
        peak = value if peak is None else max(peak, value)
        worst = max(worst, peak - value)
        if peak > 0:
            percent = max(percent, (peak - value) / peak * 100)
    return worst, percent


def metrics(state, engine, equity):
    baseline = Decimal(engine['starting_equity'])
    trades = state['trades'][engine['start_trade_count']:]
    closed = completed_positions(trades)
    wins = [t['net'] for t in closed if t['net'] > 0]
    losses = [t['net'] for t in closed if t['net'] < 0]
    fees = sum((Decimal(t['fee']) for t in trades), ZERO)
    slip = sum((Decimal(t.get('slippage_cost') or '0') for t in trades), ZERO)
    realized = sum((Decimal(t['realized_pnl']) for t in trades), ZERO)
    gross_realized = sum((Decimal(t.get('gross_pnl') or t['realized_pnl']) for t in trades if t['side'] == 'SELL'), ZERO)
    return_usd = equity - baseline if equity is not None else None
    curve = [str(baseline)] + [r['equity'] for r in engine['equity_curve']]
    dd, dd_pct = drawdown(curve + [str(equity) if equity is not None else None])
    by_strategy = {}
    for trade in closed:
        group = by_strategy.setdefault(trade['strategy'], dict(net_pnl=ZERO, closed_positions=0, winning=0))
        group['net_pnl'] += trade['net']
        group['closed_positions'] += 1
        group['winning'] += int(trade['net'] > 0)
    for group in by_strategy.values():
        group['net_pnl'] = str(group['net_pnl'])
        group['win_rate_pct'] = str(Decimal(group['winning']) / group['closed_positions'] * 100)
    win_total, loss_total = sum(wins, ZERO), -sum(losses, ZERO)
    return dict(starting_equity=str(baseline), current_equity=str(equity) if equity is not None else None,
                total_return_usd=str(return_usd) if return_usd is not None else None,
                total_return_pct=str(return_usd / baseline * 100) if return_usd is not None and baseline else None,
                total_trades=len(trades), closed_positions=len(closed), winning_trades=len(wins), losing_trades=len(losses),
                win_rate=str(Decimal(len(wins)) / len(closed)) if closed else '0',
                average_win=str(win_total / len(wins)) if wins else '0',
                average_loss=str(-loss_total / len(losses)) if losses else '0',
                profit_factor=str(win_total / loss_total) if loss_total else None,
                profit_factor_status='defined' if loss_total else 'no_losses_denominator_zero',
                expectancy=str(sum((t['net'] for t in closed), ZERO) / len(closed)) if closed else '0',
                maximum_drawdown_usd=str(dd), maximum_drawdown_pct=str(dd_pct),
                gross_pnl=str(gross_realized), realized_net_pnl=str(realized), fees=str(fees),
                slippage_cost=str(slip), net_pnl=str(return_usd) if return_usd is not None else None,
                average_holding_time_seconds=str(sum((t['holding_seconds'] for t in closed), ZERO) / len(closed)) if closed else '0',
                strategy_performance=by_strategy, open_positions=len(state['positions']),
                valuation='depth_liquidation_after_exit_fee' if equity is not None else 'unavailable',
                lifetime_starting_balance=state['starting_balance'], lifetime_realized_pnl=state['realized_pnl'])

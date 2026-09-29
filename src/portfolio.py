"""Exact decimal cash and weighted-average cost basis; no short positions."""
from copy import deepcopy
from decimal import Decimal, localcontext

from .models import decimal, timestamp, parse_time

LEGACY_TRADE_FIELDS = ('trade_id timestamp market_id market_title outcome side quantity requested_price '
                'simulated_fill_price spread fee fee_status fee_source fee_rate slippage '
                'position_before position_after cash_before cash_after realized_pnl reason').split()
EXTRA_TRADE_FIELDS = ('strategy signal_score combined_score entry_reason exit_reason holding_seconds '
                      'entry_bid entry_ask exit_bid exit_ask entry_spread exit_spread '
                      'gross_pnl net_pnl return_pct momentum order_book_imbalance liquidity volume '
                      'risk_check_result decision_id requested_quantity fill_status fill_notional '
                      'fill_legs slippage_cost quote_timestamp book_timestamp risk_budget '
                      'fair_probability model_confidence source_quality edge kelly_fraction ai_model '
                      'grounding_sources free_strategy_version').split()
TRADE_FIELDS = LEGACY_TRADE_FIELDS + EXTRA_TRADE_FIELDS


def gross_cost_basis(state, market_id, outcome):
    quantity, cost = Decimal('0'), Decimal('0')
    for trade in state['trades']:
        if trade['market_id'] != market_id or trade['outcome'] != outcome:
            continue
        q = Decimal(trade['quantity'])
        if trade['side'] == 'BUY':
            quantity += q
            cost += Decimal(trade.get('fill_notional') or str(q * Decimal(trade['simulated_fill_price'])))
        else:
            cost = Decimal('0') if q == quantity else cost * (quantity - q) / quantity
            quantity -= q
    return quantity, cost


def initial_state(balance):
    return dict(schema_version=1, mode='PAPER_ONLY', starting_balance=str(balance),
                start_time=timestamp(), cash_balance=str(balance), positions={},
                realized_pnl='0', trades=[], test_run=None)


def apply_fill(state, market_id, title, outcome, side, quantity, price, fee, fill_notional=None):
    # Keep cash/products exact at supported input precision; round cost allocation only.
    with localcontext() as ctx:
        ctx.prec = 60
        cash, realized = Decimal(state['cash_balance']), Decimal(state['realized_pnl'])
        key = market_id + ':' + outcome
        old = state['positions'].get(key)
        before = Decimal(old['quantity']) if old else Decimal('0')
        cost = Decimal(old['cost_basis']) if old else Decimal('0')
        notional = quantity * price if fill_notional is None else fill_notional
        if side == 'BUY':
            debit = notional + fee
            if debit > cash:
                raise ValueError('insufficient paper cash')
            after, cash_after, pnl = before + quantity, cash - debit, Decimal('0')
            new_cost = cost + debit
        elif side == 'SELL':
            if quantity > before:
                raise ValueError('insufficient paper position')
            allocated = cost if quantity == before else (cost * quantity / before).quantize(Decimal('0.000000000000000001'))
            proceeds = notional - fee
            after, cash_after, pnl = before - quantity, cash + proceeds, proceeds - allocated
            if cash_after < 0:
                raise ValueError('insufficient cash for fee')
            new_cost = cost - allocated
        else:
            raise ValueError('unknown side')
        if after:
            state['positions'][key] = dict(market_id=market_id, market_title=title,
                                           outcome=outcome, quantity=str(after), cost_basis=str(new_cost))
        else:
            state['positions'].pop(key, None)
        state['cash_balance'], state['realized_pnl'] = str(cash_after), str(realized + pnl)
        return dict(position_before=str(before), position_after=str(after), cash_before=str(cash),
                    cash_after=str(cash_after), realized_pnl=str(pnl))


def validate_state(state):
    """Replay ledger and reject syntactically valid but inconsistent state."""
    if state['schema_version'] != 1 or state['mode'] != 'PAPER_ONLY':
        raise ValueError('unsupported state version/mode')
    start = decimal(state['starting_balance'])
    if start != Decimal('50.00'):
        raise ValueError('unexpected starting balance')
    parse_time(state['start_time'])
    replay = initial_state(start)
    for index, trade in enumerate(state['trades'], 1):
        if (not set(LEGACY_TRADE_FIELDS) <= set(trade) <= set(TRADE_FIELDS)
                or trade['trade_id'] != index):
            raise ValueError('invalid trade schema/sequence')
        parse_time(trade['timestamp'])
        qty, price, fee = (decimal(trade[k]) for k in ('quantity', 'simulated_fill_price', 'fee'))
        if qty <= 0 or not 0 < price < 1 or fee < 0 or trade['outcome'] not in ('YES', 'NO'):
            raise ValueError('invalid ledger quantity/price/fee/outcome')
        if trade['fee_status'] not in ('known', 'free'):
            raise ValueError('filled trade without known fee')
        notional = None
        if trade.get('fill_legs'):
            legs = trade['fill_legs']
            if not isinstance(legs, list) or any(decimal(p) <= 0 or decimal(p) >= 1 or decimal(q) <= 0 for p, q in legs):
                raise ValueError('invalid fill legs')
            with localcontext() as ctx:
                ctx.prec = 60
                notional = sum((decimal(p) * decimal(q) for p, q in legs), Decimal('0'))
                if sum((decimal(q) for _, q in legs), Decimal('0')) != qty or notional != Decimal(trade['fill_notional']):
                    raise ValueError('fill legs mismatch')
                if abs(notional / qty - price) > Decimal('0.000000000000000001'):
                    raise ValueError('VWAP mismatch')
        actual = apply_fill(replay, trade['market_id'], trade['market_title'], trade['outcome'],
                            trade['side'], qty, price, fee, notional)
        if actual != {k: trade[k] for k in actual}:
            raise ValueError('trade accounting mismatch')
    if any(replay[k] != state[k] for k in ('cash_balance', 'positions', 'realized_pnl')):
        raise ValueError('state does not match trade ledger')
    if 'strategy_state' in state:
        from .strategy.strategy_engine import validate_engine_state
        validate_engine_state(state['strategy_state'])
    test = state['test_run']
    if test is not None:
        if test['stage'] not in ('bought', 'completed') or test['outcome'] != 'YES':
            raise ValueError('invalid test checkpoint')
        if test['stage'] == 'bought':
            pos = state['positions'].get(test['market_id'] + ':YES')
            if not pos or Decimal(pos['quantity']) < decimal(test['quantity']):
                raise ValueError('test checkpoint position missing')


class Portfolio:
    def __init__(self, state):
        validate_state(state)
        self.state = state

    def positions(self):
        return deepcopy(self.state['positions'])

    def valuation(self, markets, max_age):
        total, cost = Decimal('0'), Decimal('0')
        missing = []
        with localcontext() as ctx:
            ctx.prec = 60
            for key, position in self.state['positions'].items():
                market = markets.get(position['market_id'])
                quote = market.quotes.get(position['outcome']) if market else None
                try:
                    if quote is None or quote.bid is None:
                        raise ValueError('bid unavailable')
                    quote.require_fresh(max_age)
                    total += Decimal(position['quantity']) * quote.bid
                    cost += Decimal(position['cost_basis'])
                except ValueError:
                    missing.append(key)
            return dict(equity=None if missing else str(Decimal(self.state['cash_balance']) + total),
                        unrealized_pnl=None if missing else str(total - cost),
                        valuation_status='unavailable' if missing else 'fresh_bid_before_exit_fee',
                        unpriced_positions=missing)

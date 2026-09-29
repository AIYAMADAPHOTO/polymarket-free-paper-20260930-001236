from copy import deepcopy
from decimal import Decimal, localcontext
import logging

from config import STARTING_BALANCE_USD, MAX_QUOTE_AGE_SECONDS
from .models import decimal, timestamp, parse_time, utcnow
from .portfolio import Portfolio, apply_fill, gross_cost_basis, EXTRA_TRADE_FIELDS


class BestQuoteFill:
    """Replaceable Phase 2 seam. Reject oversize instead of inventing liquidity."""
    def fill(self, side, quantity, quote):
        price, size = (quote.ask, quote.ask_size) if side == 'BUY' else (quote.bid, quote.bid_size)
        if price is None or size is None:
            raise ValueError('required order book side unavailable')
        if quantity > size:
            raise ValueError('quantity exceeds displayed best-level size')
        return price, Decimal('0')


class PaperBroker:
    def __init__(self, storage, fill_model=None):
        self.storage = storage
        self.portfolio = Portfolio(storage.load(STARTING_BALANCE_USD))
        self.fill_model = fill_model or BestQuoteFill()
        self.log = logging.getLogger('paperbot')
        self.markets = {}
        # Repairs a CSV missing after a commit or crash; never duplicates trades.
        storage.export_trades(self.portfolio.state)

    @property
    def state(self):
        return deepcopy(self.portfolio.state)

    def mark(self, markets):
        self.markets = {m.market_id: m for m in markets}

    def buy(self, market, outcome, quantity, requested_price=None, reason='manual paper BUY', test_checkpoint=None,
            trade_context=None, state_transform=None, allow_partial=False):
        return self._trade(market, outcome, 'BUY', quantity, requested_price, reason, test_checkpoint,
                           trade_context, state_transform, allow_partial)

    def sell(self, market, outcome, quantity, requested_price=None, reason='manual paper SELL', test_checkpoint=None,
             trade_context=None, state_transform=None, allow_partial=False):
        return self._trade(market, outcome, 'SELL', quantity, requested_price, reason, test_checkpoint,
                           trade_context, state_transform, allow_partial)

    def close_position(self, market, outcome, reason='manual paper close'):
        position = self.portfolio.state['positions'].get(market.market_id + ':' + outcome.upper())
        if not position:
            raise ValueError('no open position')
        return self.sell(market, outcome, position['quantity'], reason=reason)

    def _trade(self, market, outcome, side, quantity, requested_price, reason, test_checkpoint,
               trade_context=None, state_transform=None, allow_partial=False):
        with localcontext() as ctx:
            ctx.prec = 60
            outcome, quantity = outcome.upper(), decimal(quantity)
            if outcome not in ('YES', 'NO') or quantity <= 0:
                raise ValueError('invalid outcome/quantity')
            if market.status != 'active_accepting_orders':
                raise ValueError('market unavailable')
            if market.end_time and parse_time(market.end_time) <= utcnow():
                raise ValueError('market end time passed')
            age = (utcnow() - parse_time(market.fetched_at)).total_seconds()
            if not -5 <= age <= MAX_QUOTE_AGE_SECONDS:
                raise ValueError('stale market metadata')
            quote = market.quotes.get(outcome)
            if quote is None or quote.token_id != market.tokens[outcome]:
                raise ValueError('quote unavailable/mismatched')
            quote.require_fresh(MAX_QUOTE_AGE_SECONDS)
            execution = None
            if hasattr(self.fill_model, 'execute'):
                execution = self.fill_model.execute(side, quantity, quote,
                            decimal(requested_price) if requested_price is not None else None, allow_partial)
                quantity, price, slippage = execution.quantity, execution.price, execution.slippage
            else:
                price, slippage = self.fill_model.fill(side, quantity, quote)
            if not price.is_finite() or not 0 < price < 1 or slippage < 0:
                raise ValueError('invalid fill model result')
            requested = decimal(requested_price) if requested_price is not None else price
            if not 0 < requested < 1:
                raise ValueError('invalid requested price')
            if (side == 'BUY' and price > requested) or (side == 'SELL' and price < requested):
                raise ValueError('paper limit price would not fill')
            try:
                fee = execution.fee(market.fee) if execution else market.fee.calculate(quantity, price)
            except ValueError:
                self.log.warning('Paper %s refused market=%s fee status = unknown', side, market.market_id)
                raise
            candidate = self.state
            old_quantity, old_gross = gross_cost_basis(candidate, market.market_id, outcome)
            old_cost = Decimal(candidate['positions'].get(market.market_id + ':' + outcome, {}).get('cost_basis', '0'))
            notional = execution.notional if execution else quantity * price
            changes = apply_fill(candidate, market.market_id, market.title, outcome, side, quantity, price, fee,
                                 notional if execution else None)
            trade = dict(trade_id=len(candidate['trades']) + 1, timestamp=timestamp(),
                         market_id=market.market_id, market_title=market.title, outcome=outcome, side=side,
                         quantity=str(quantity), requested_price=str(requested), simulated_fill_price=str(price),
                         spread=str(quote.spread) if quote.spread is not None else None, fee=str(fee),
                         fee_status=market.fee.status, fee_source=market.fee.source,
                         fee_rate=str(market.fee.rate) if market.fee.rate is not None else None,
                         slippage=str(slippage), **changes, reason=reason)
            if trade_context:
                if not set(trade_context) <= set(EXTRA_TRADE_FIELDS):
                    raise ValueError('invalid trade context fields')
                trade.update(trade_context)
            if execution:
                allocated_gross = old_gross * quantity / old_quantity if side == 'SELL' else Decimal('0')
                allocated_cost = old_cost * quantity / old_quantity if side == 'SELL' else Decimal('0')
                trade.update(requested_quantity=str(execution.requested_quantity), fill_status=execution.status,
                             fill_notional=str(notional), fill_legs=[[str(p), str(q)] for p, q in execution.legs],
                             slippage_cost=str(execution.slippage_cost), quote_timestamp=quote.fetched_at,
                             book_timestamp=quote.book_timestamp,
                             gross_pnl=str(notional - allocated_gross) if side == 'SELL' else '0',
                             net_pnl=changes['realized_pnl'],
                             return_pct=str(Decimal(changes['realized_pnl']) / allocated_cost * 100) if allocated_cost else None)
            candidate['trades'].append(trade)
            if test_checkpoint is not None:
                candidate['test_run'] = test_checkpoint
            if state_transform:
                state_transform(candidate, trade)
            self.storage.save(candidate)
            self.portfolio.state = candidate  # Commit point is atomic state replacement.
            self.storage.export_trades(candidate)
            self.markets[market.market_id] = market
            self.log.info('Paper %s market=%s outcome=%s qty=%s price=%s fee=%s cash=%s realized PnL=%s',
                          side, market.market_id, outcome, quantity, price, fee,
                          changes['cash_after'], candidate['realized_pnl'])
            self.record_portfolio()
            return trade

    def save_strategy_state(self, strategy_state):
        candidate = self.state
        candidate['strategy_state'] = deepcopy(strategy_state)
        self.storage.save(candidate)
        self.portfolio.state = candidate

    def save_free_strategy_state(self, free_state):
        """Persist the free/original-post strategy state without touching legacy Phase 2 state."""
        candidate = self.state
        candidate['free_strategy_state'] = deepcopy(free_state)
        self.storage.save(candidate)
        self.portfolio.state = candidate

    def get_cash_balance(self):
        return Decimal(self.portfolio.state['cash_balance'])

    def get_positions(self):
        return self.portfolio.positions()

    def get_equity(self):
        result = self.valuation()['equity']
        return None if result is None else Decimal(result)

    def get_realized_pnl(self):
        return Decimal(self.portfolio.state['realized_pnl'])

    def get_unrealized_pnl(self):
        result = self.valuation()['unrealized_pnl']
        return None if result is None else Decimal(result)

    def valuation(self):
        return self.portfolio.valuation(self.markets, MAX_QUOTE_AGE_SECONDS)

    def record_portfolio(self):
        value = self.valuation()
        self.storage.append_portfolio(self.portfolio.state, value)
        self.log.info('PnL realized=%s unrealized=%s equity=%s status=%s',
                      self.get_realized_pnl(), value['unrealized_pnl'], value['equity'], value['valuation_status'])

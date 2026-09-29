from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json

ZERO = Decimal('0')


def utcnow():
    return datetime.now(timezone.utc)


def timestamp():
    return utcnow().isoformat()


def parse_time(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('timezone missing')
    return dt


def decimal(value):
    if isinstance(value, (float, bool)):
        raise ValueError('Use decimal strings, not float/bool')
    try:
        d = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('Invalid decimal') from exc
    if not d.is_finite() or len(d.as_tuple().digits) > 24 or abs(d.as_tuple().exponent) > 18:
        raise ValueError('Nonfinite or excessive precision decimal')
    return d


def optional_decimal(value):
    return None if value is None or value == '' else decimal(value)


@dataclass(frozen=True)
class FeeInfo:
    status: str = 'unknown'
    rate: Decimal | None = None
    source: str = 'unavailable'
    exponent: int | None = None

    def calculate(self, quantity, price):
        if self.status == 'free':
            return ZERO
        if self.status != 'known' or self.rate is None or self.exponent != 1:
            raise ValueError('fee status = unknown; paper fill refused')
        # Official current taker formula. Cash-denominated paper fee on both sides.
        raw = quantity * self.rate * price * (1 - price)
        if raw < Decimal('0.00001'):
            return ZERO
        return raw.quantize(Decimal('0.00001'), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Quote:
    token_id: str
    bid: Decimal | None
    ask: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    fetched_at: str
    book_timestamp: str | None = None
    bids: tuple | None = None
    asks: tuple | None = None
    book_hash: str | None = None

    @property
    def spread(self):
        return None if self.bid is None or self.ask is None else self.ask - self.bid

    def require_fresh(self, max_age):
        age = (utcnow() - parse_time(self.fetched_at)).total_seconds()
        if not -5 <= age <= max_age:
            raise ValueError('stale/future quote receipt')
        if self.book_timestamp is None:
            raise ValueError('book timestamp unavailable')
        raw = decimal(self.book_timestamp)
        seconds = raw / 1000 if raw > Decimal('100000000000') else raw
        age = Decimal(str(utcnow().timestamp())) - seconds
        if not -5 <= age <= max_age:
            raise ValueError('stale/future order book')


@dataclass
class Market:
    market_id: str
    condition_id: str
    title: str
    event_title: str
    tokens: dict
    prices: dict
    volume: Decimal | None
    liquidity: Decimal | None
    end_time: str | None
    fetched_at: str
    fee: FeeInfo
    quotes: dict = field(default_factory=dict)
    status: str = 'active_accepting_orders'

    def snapshot(self):
        row = dict(timestamp=self.fetched_at, market_id=self.market_id, title=self.title,
                   event_title=self.event_title, yes_price=self.prices.get('YES'),
                   no_price=self.prices.get('NO'), volume=self.volume, liquidity=self.liquidity,
                   end_time=self.end_time, market_status=self.status,
                   fee_status=self.fee.status, fee_rate=self.fee.rate, fee_source=self.fee.source)
        for outcome in ('YES', 'NO'):
            q = self.quotes.get(outcome)
            for field_name in ('bid', 'ask', 'bid_size', 'ask_size', 'book_timestamp', 'fetched_at'):
                row[outcome.lower() + '_' + field_name] = getattr(q, field_name) if q else None
        q = self.quotes.get('YES')
        row['spread'] = q.spread if q else None
        q = self.quotes.get('NO')
        row['no_spread'] = q.spread if q else None
        for outcome in ('YES', 'NO'):
            q = self.quotes.get(outcome)
            for name in ('bids', 'asks'):
                levels = getattr(q, name) if q else None
                row[outcome.lower() + '_' + name] = (json.dumps([[str(p), str(s)] for p, s in levels])
                                                    if levels is not None else None)
            row[outcome.lower() + '_book_hash'] = q.book_hash if q else None
        return row

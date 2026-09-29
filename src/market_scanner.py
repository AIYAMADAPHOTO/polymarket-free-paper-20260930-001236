import json
import logging
import re
from decimal import Decimal

from config import PAGE_SIZE, MAX_PAGES
from .models import Market, Quote, FeeInfo, decimal, optional_decimal, parse_time, utcnow, timestamp
from .polymarket_client import ApiError


def fee_info(raw):
    schedule = raw.get('feeSchedule')
    enabled = raw.get('feesEnabled')
    try:
        if enabled is False:
            if schedule and decimal(schedule.get('rate', '0')) != 0:
                return FeeInfo(source='conflicting Gamma fee fields')
            return FeeInfo('free', Decimal('0'), 'Gamma feesEnabled=false', 1)
        if isinstance(schedule, dict) and enabled is True:
            rate = decimal(schedule['rate'])
            exponent = decimal(schedule['exponent'])
            if 0 <= rate <= 1 and exponent == 1:
                return FeeInfo('known', rate, 'Gamma feeSchedule', 1)
    except (ValueError, KeyError, TypeError):
        pass
    return FeeInfo(source='Gamma fee fields missing/unsupported')


def array(value):
    result = json.loads(value, parse_float=Decimal) if isinstance(value, str) else value
    if not isinstance(result, list):
        raise ValueError('expected array')
    return result


def normalize(raw):
    if not isinstance(raw, dict):
        raise ValueError('market is not an object')
    if raw.get('active') is not True or raw.get('closed') is not False:
        raise ValueError('inactive/closed/unknown status')
    if raw.get('archived') is True or raw.get('acceptingOrders') is not True or raw.get('enableOrderBook') is not True:
        raise ValueError('archived or order book not accepting')
    if raw.get('umaResolutionStatus') in ('proposed', 'resolved'):
        raise ValueError('resolution already proposed/resolved')
    end = raw.get('endDate')
    if end and parse_time(end) <= utcnow():
        raise ValueError('end time passed')
    labels = [str(v).upper() for v in array(raw.get('outcomes'))]
    if len(labels) != 2 or set(labels) != {'YES', 'NO'}:
        raise ValueError('Phase 1 supports literal YES/NO markets only')
    tokens = array(raw.get('clobTokenIds'))
    prices = array(raw.get('outcomePrices')) if raw.get('outcomePrices') else [None, None]
    if len(tokens) != 2 or len(prices) != 2 or len(set(tokens)) != 2 or not all(str(t).isdigit() for t in tokens):
        raise ValueError('invalid token/price mapping')
    prices = [optional_decimal(p) for p in prices]
    if any(p is not None and not 0 <= p <= 1 for p in prices):
        raise ValueError('outcome price outside [0,1]')
    volume, liquidity = optional_decimal(raw.get('volume')), optional_decimal(raw.get('liquidity'))
    if any(v is not None and v < 0 for v in (volume, liquidity)):
        raise ValueError('negative volume/liquidity')
    market_id, title, condition = str(raw['id']), raw['question'], raw['conditionId']
    if (not market_id.isdigit() or not isinstance(title, str) or not title or
            not isinstance(condition, str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}', condition)):
        raise ValueError('missing identity')
    events = raw.get('events') or []
    if not isinstance(events, list) or any(not isinstance(e, dict) or not isinstance(e.get('title', ''), str) for e in events):
        raise ValueError('invalid event metadata')
    return Market(market_id, condition, title, ' | '.join(e.get('title', '') for e in events),
                  dict(zip(labels, map(str, tokens))), dict(zip(labels, prices)), volume,
                  liquidity, end, timestamp(), fee_info(raw))


def normalize_book(raw, token, condition):
    if not isinstance(raw, dict) or str(raw.get('asset_id')) != token or raw.get('market') != condition:
        raise ValueError('order book identity mismatch')
    sides, depth = [], []
    for name, choose in (('bids', max), ('asks', min)):
        levels = raw.get(name)
        if not isinstance(levels, list):
            raise ValueError('invalid order book schema')
        valid = []
        for item in levels:
            p, size = decimal(item['price']), decimal(item['size'])
            if not 0 < p < 1 or size < 0:
                raise ValueError('invalid book price/size')
            if size > 0:
                valid.append((p, size))
        price = choose(p for p, _ in valid) if valid else None
        size = sum((s for p, s in valid if p == price), Decimal('0')) if valid else None
        sides.append((price, size))
        merged = {}
        for p, s in valid:
            merged[p] = merged.get(p, Decimal('0')) + s
        depth.append(tuple(sorted(merged.items(), reverse=name == 'bids')))
    (bid, bs), (ask, ass) = sides
    if bid is not None and ask is not None and bid > ask:
        raise ValueError('crossed book')
    return Quote(token, bid, ask, bs, ass, timestamp(), raw.get('timestamp'),
                 depth[0], depth[1], raw.get('hash'))


class MarketScanner:
    def __init__(self, client, storage, max_markets=20, max_pages=MAX_PAGES, page_size=PAGE_SIZE):
        self.client, self.storage = client, storage
        self.max_markets, self.max_pages, self.page_size = max_markets, max_pages, page_size
        self.log = logging.getLogger('paperbot')
        self.stats = {}

    def enrich(self, market):
        for outcome, token in market.tokens.items():
            try:
                market.quotes[outcome] = normalize_book(self.client.book(token), token, market.condition_id)
            except (ApiError, ValueError, KeyError, TypeError) as exc:
                self.log.warning('Book unavailable market=%s outcome=%s: %s', market.market_id, outcome, exc)
        if market.fee.status == 'unknown':
            try:
                fd = self.client.fee_details(market.condition_id).get('fd', {})
                r, e = decimal(fd['r']), decimal(fd['e'])
                if 0 <= r <= 1 and e == 1:
                    market.fee = FeeInfo('known', r, 'CLOB fd', 1)
            except (ApiError, ValueError, KeyError, TypeError, AttributeError) as exc:
                self.log.warning('fee status = unknown market=%s: %s', market.market_id, exc)
        if market.fee.status == 'unknown':
            self.log.warning('fee status = unknown market=%s source=%s', market.market_id, market.fee.source)
        self.storage.append_snapshot(market.snapshot())
        return market

    def fetch_market(self, market_id):
        return self.enrich(normalize(self.client.market(market_id)))

    def scan(self):
        found, seen, cursors = [], set(), set()
        fetched = excluded = duplicates = 0
        cursor = None
        exhausted = False
        for _ in range(self.max_pages):
            page = self.client.market_page(self.page_size, cursor)
            for raw in page['markets']:
                fetched += 1
                mid = str(raw.get('id', '')) if isinstance(raw, dict) else ''
                if mid in seen:
                    duplicates += 1
                    continue
                seen.add(mid)
                try:
                    market = normalize(raw)
                except (ValueError, KeyError, TypeError) as exc:
                    excluded += 1
                    self.log.info('Excluded market=%s reason=%s', mid, exc)
                    self.storage.append_csv('scanner_exclusions.csv', dict(timestamp=timestamp(), market_id=mid,
                                            title=raw.get('question', '') if isinstance(raw, dict) else '', reason=str(exc)))
                    continue
                found.append(self.enrich(market))
                if len(found) >= self.max_markets:
                    break
            if len(found) >= self.max_markets:
                break
            cursor = page.get('next_cursor')
            if not cursor:
                exhausted = True
                break
            if cursor in cursors:
                raise ApiError('Gamma returned repeated pagination cursor')
            cursors.add(cursor)
        self.stats = dict(inspected=fetched, accepted=len(found), excluded=excluded,
                          duplicates=duplicates, all_pages_exhausted=exhausted)
        self.log.info('Market scan %s (bounded sample, not global total)', self.stats)
        return found

"""Cheap metadata-first scan for the zero-cost original-post-style bot."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import logging
import re

from .market_scanner import normalize
from .models import optional_decimal, parse_time, utcnow
from .polymarket_client import ApiError


POLITICAL_PATTERNS = (
    r'\belection\b', r'\bpresident(?:ial)?\b', r'\bvice president\b', r'\bsenate\b',
    r'\bcongress\b', r'\bhouse of representatives\b', r'\bgovernor\b', r'\bmayor\b',
    r'\bparliament\b', r'\bprime minister\b', r'\bpolitic(?:s|al)\b', r'\bballot\b',
    r'\breferendum\b', r'\bvote(?:r|s|d|ing)?\b', r'\bcandidate\b', r'\bparty\b',
    r'\bimpeach(?:ment|ed)?\b', r'\blegislation\b', r'\bbill\b.*\bpass\b',
    r'\bminister\b', r'\bmember of parliament\b', r'\bMP\b',
)


@dataclass(frozen=True)
class BroadCandidate:
    market: object
    description: str
    category: str
    tags: tuple
    volume24hr: Decimal | None
    raw_best_bid: Decimal | None
    raw_best_ask: Decimal | None
    source_updated_at: str | None

    @property
    def market_id(self):
        return self.market.market_id

    @property
    def title(self):
        return self.market.title

    @property
    def yes_price(self):
        return self.market.prices.get('YES')

    def political(self):
        haystack = ' '.join((self.title, self.market.event_title, self.description,
                             self.category, *self.tags)).lower()
        if self.category.lower() in {'politics', 'elections', 'government'}:
            return True
        return any(re.search(pattern, haystack, re.I) for pattern in POLITICAL_PATTERNS)

    def ai_payload(self):
        return {
            'market_id': self.market_id,
            'question': self.title,
            'event': self.market.event_title,
            'description': self.description[:1200],
            'category': self.category,
            'tags': list(self.tags)[:12],
            'end_time': self.market.end_time,
            'market_yes_price_reference_only': str(self.yes_price) if self.yes_price is not None else None,
            'volume': str(self.market.volume) if self.market.volume is not None else None,
            'volume24hr': str(self.volume24hr) if self.volume24hr is not None else None,
            'liquidity': str(self.market.liquidity) if self.market.liquidity is not None else None,
        }


def _tags(raw):
    values = []
    for tag in raw.get('tags') or []:
        if isinstance(tag, dict):
            value = tag.get('label') or tag.get('slug')
            if value:
                values.append(str(value))
    for event in raw.get('events') or []:
        if isinstance(event, dict):
            for tag in event.get('tags') or []:
                if isinstance(tag, dict):
                    value = tag.get('label') or tag.get('slug')
                    if value:
                        values.append(str(value))
    return tuple(dict.fromkeys(values))


def candidate_from_raw(raw):
    market = normalize(raw)
    return BroadCandidate(
        market=market,
        description=str(raw.get('description') or ''),
        category=str(raw.get('category') or ''),
        tags=_tags(raw),
        volume24hr=optional_decimal(raw.get('volume24hr')),
        raw_best_bid=optional_decimal(raw.get('bestBid')),
        raw_best_ask=optional_decimal(raw.get('bestAsk')),
        source_updated_at=raw.get('updatedAt'),
    )


class BroadMarketScanner:
    """Scans up to 1,000 Gamma markets without fetching 2,000 order books.

    Only markets that survive the metadata prefilter are later enriched from the CLOB.
    This is the key change that makes the original-post workflow practical on a free VPS.
    """
    def __init__(self, client, config):
        self.client = client
        self.config = config
        self.log = logging.getLogger('paperbot.free')
        self.stats = {}

    def scan(self):
        cfg = self.config
        candidates, seen, cursors = [], set(), set()
        inspected = excluded = duplicates = 0
        cursor = None
        pages = 0
        while len(candidates) < cfg.scan_limit and pages < 20:
            page = self.client.market_page(
                cfg.page_size, cursor, order='volume_num,liquidity_num', ascending=False, include_tag=True)
            pages += 1
            markets = page.get('markets')
            if not isinstance(markets, list):
                raise ApiError('Invalid Gamma broad market page')
            for raw in markets:
                inspected += 1
                mid = str(raw.get('id', '')) if isinstance(raw, dict) else ''
                if mid in seen:
                    duplicates += 1
                    continue
                seen.add(mid)
                try:
                    candidate = candidate_from_raw(raw)
                except (ValueError, KeyError, TypeError) as exc:
                    excluded += 1
                    self.log.debug('Broad exclusion market=%s reason=%s', mid, exc)
                    continue
                candidates.append(candidate)
                if len(candidates) >= cfg.scan_limit:
                    break
            cursor = page.get('next_cursor')
            if not cursor:
                break
            if cursor in cursors:
                raise ApiError('Gamma returned repeated pagination cursor')
            cursors.add(cursor)
        self.stats = {
            'inspected': inspected,
            'accepted_metadata': len(candidates),
            'excluded_invalid': excluded,
            'duplicates': duplicates,
            'pages': pages,
            'requested_limit': cfg.scan_limit,
            'exhausted': not bool(cursor),
        }
        return candidates

    def prefilter(self, candidates, held_market_ids=()):
        cfg = self.config
        held = set(held_market_ids)
        accepted, rejected = [], []
        now = utcnow()
        for c in candidates:
            reasons = []
            if c.political():
                reasons.append('political_market_excluded')
            p = c.yes_price
            if p is None or not cfg.min_price <= p <= cfg.max_price:
                reasons.append('price_outside_prefilter')
            if c.market.volume is None or c.market.volume < cfg.min_volume:
                reasons.append('volume_below_prefilter')
            if c.market.liquidity is None or c.market.liquidity < cfg.min_liquidity:
                reasons.append('liquidity_below_prefilter')
            if c.market.end_time:
                try:
                    remaining = (parse_time(c.market.end_time) - now).total_seconds()
                    if remaining < cfg.min_remaining_seconds:
                        reasons.append('too_close_to_end')
                except ValueError:
                    reasons.append('invalid_end_time')
            if reasons and c.market_id not in held:
                rejected.append((c, tuple(reasons)))
            elif not c.political():
                accepted.append(c)

        def ranking(c):
            # Decimal-only stable ranking: recent volume matters most, then liquidity/total volume.
            return (
                c.volume24hr or Decimal('0'),
                c.market.liquidity or Decimal('0'),
                c.market.volume or Decimal('0'),
            )

        normal = sorted((c for c in accepted if c.market_id not in held), key=ranking, reverse=True)
        held_rows = [c for c in accepted if c.market_id in held]
        room = max(0, cfg.ai_candidate_limit - len(held_rows))
        selected = held_rows + normal[:room]
        return selected, rejected

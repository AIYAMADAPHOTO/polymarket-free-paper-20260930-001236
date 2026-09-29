"""Zero-cost current-evidence collection for the free fair-value bot.

No paid search/news API is used. General/sports/weather freshness comes from the
public Google News RSS search feed. A small crypto adapter adds public Kraken ticker
information when the market title clearly names a supported asset.

All retrieved text is treated as untrusted evidence and is only passed to Gemini as
data. Failures produce empty evidence; they never trigger a paid fallback.
"""
from __future__ import annotations

from datetime import datetime, timezone
import email.utils
import json
import logging
from pathlib import Path
import re
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from .models import parse_time, utcnow
from .storage import atomic_write


NEWS_RSS = 'https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en'
KRAKEN_TICKER = 'https://api.kraken.com/0/public/Ticker?{params}'
CRYPTO_PAIRS = (
    (re.compile(r'\b(?:bitcoin|btc)\b', re.I), 'XBTUSD', 'BTC/USD'),
    (re.compile(r'\b(?:ethereum|ether|eth)\b', re.I), 'ETHUSD', 'ETH/USD'),
    (re.compile(r'\b(?:solana|sol)\b', re.I), 'SOLUSD', 'SOL/USD'),
    (re.compile(r'\b(?:xrp|ripple)\b', re.I), 'XRPUSD', 'XRP/USD'),
    (re.compile(r'\b(?:dogecoin|doge)\b', re.I), 'DOGEUSD', 'DOGE/USD'),
)


def _category(candidate):
    text = ' '.join((candidate.category, *candidate.tags, candidate.title)).lower()
    if any(k in text for k in ('sport', 'nba', 'nfl', 'mlb', 'nhl', 'soccer', 'football', 'tennis', 'ufc')):
        return 'sports'
    if any(k in text for k in ('weather', 'temperature', 'rain', 'snow', 'hurricane', 'storm', 'climate')):
        return 'weather'
    if any(p.search(text) for p, _, _ in CRYPTO_PAIRS) or any(k in text for k in ('crypto', 'cryptocurrency')):
        return 'crypto'
    return 'general'


def _news_query(candidate):
    base = candidate.title.strip()
    kind = _category(candidate)
    if kind == 'sports':
        return f'{base} injury lineup availability'
    if kind == 'weather':
        return f'{base} NOAA NWS forecast weather'
    if kind == 'crypto':
        return f'{base} crypto market latest'
    return base


def _parse_rfc822(value):
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_text(node, tag):
    item = node.find(tag)
    return (item.text or '').strip() if item is not None and item.text else ''


class FreeEvidenceCollector:
    def __init__(self, config, state_dir, opener=urlopen):
        self.config = config
        self.state_dir = Path(state_dir)
        self.cache_path = self.state_dir / 'free_evidence_cache.json'
        self.opener = opener
        self.log = logging.getLogger('paperbot.free.evidence')
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _load_cache(self):
        if not self.cache_path.exists():
            return {'version': 1, 'markets': {}}
        try:
            data = json.loads(self.cache_path.read_text(encoding='utf-8'))
            if data.get('version') != 1 or not isinstance(data.get('markets'), dict):
                raise ValueError('bad evidence cache schema')
            return data
        except (OSError, ValueError, TypeError):
            # Cache is non-authoritative. Preserve it for inspection and start clean.
            backup = self.cache_path.with_suffix('.corrupt.json')
            try:
                if not backup.exists():
                    backup.write_bytes(self.cache_path.read_bytes())
            except OSError:
                pass
            return {'version': 1, 'markets': {}}

    def _save_cache(self, cache):
        atomic_write(self.cache_path, json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + '\n')

    def _fresh(self, row, now):
        try:
            fetched = parse_time(row['fetched_at'])
            return (now - fetched).total_seconds() < self.config.evidence_cache_seconds
        except (KeyError, TypeError, ValueError):
            return False

    def _open(self, url):
        req = Request(url, headers={'User-Agent': 'PolymarketPaperBot-Free/1.0'})
        with self.opener(req, timeout=self.config.evidence_request_timeout_seconds) as response:
            return response.read()

    def _news(self, candidate):
        query = _news_query(candidate)
        url = NEWS_RSS.format(query=quote_plus(query))
        raw = self._open(url)
        root = ElementTree.fromstring(raw)
        rows = []
        for item in root.findall('./channel/item')[:self.config.evidence_items_per_market]:
            source = item.find('source')
            rows.append({
                'kind': 'news_rss',
                'title': _safe_text(item, 'title')[:500],
                'published_at': _parse_rfc822(_safe_text(item, 'pubDate')),
                'url': _safe_text(item, 'link')[:1200],
                'source': ((source.text or '').strip() if source is not None and source.text else '')[:200],
                'source_url': (source.attrib.get('url', '') if source is not None else '')[:1200],
            })
        return query, rows

    def _crypto(self, candidate):
        text = candidate.title + ' ' + candidate.description
        match = next(((pair, label) for pattern, pair, label in CRYPTO_PAIRS if pattern.search(text)), None)
        if not match:
            return []
        pair, label = match
        raw = json.loads(self._open(KRAKEN_TICKER.format(params=urlencode({'pair': pair}))).decode('utf-8'))
        result = raw.get('result') or {}
        if raw.get('error') or not result:
            return []
        ticker = next(iter(result.values()))
        last = ticker.get('c', [None])[0]
        high = ticker.get('h', [None, None])[-1]
        low = ticker.get('l', [None, None])[-1]
        volume = ticker.get('v', [None, None])[-1]
        return [{
            'kind': 'crypto_public_ticker',
            'source': 'Kraken public market data',
            'pair': label,
            'last': last,
            'high_24h': high,
            'low_24h': low,
            'volume_24h': volume,
            'observed_at': utcnow().isoformat(),
        }]

    def collect(self, candidates):
        now = utcnow()
        cache = self._load_cache()
        output = {}
        fresh_fetches = 0
        errors = []
        # Only a bounded set needs external evidence each cycle. Remaining AI candidates
        # are passed with evidence_status=not_collected and the model is told to skip them.
        for candidate in list(candidates)[:self.config.evidence_candidate_limit]:
            key = str(candidate.market_id)
            cached = cache['markets'].get(key)
            if cached and self._fresh(cached, now):
                output[key] = cached
                continue
            row = {
                'market_id': key,
                'fetched_at': now.isoformat(),
                'category_hint': _category(candidate),
                'query': None,
                'items': [],
                'errors': [],
            }
            try:
                query, items = self._news(candidate)
                row['query'] = query
                row['items'].extend(items)
                fresh_fetches += 1
            except Exception as exc:  # best-effort evidence; trading still fails closed
                message = f'news_rss:{type(exc).__name__}:{str(exc)[:300]}'
                row['errors'].append(message)
                errors.append({'market_id': key, 'error': message})
            if row['category_hint'] == 'crypto':
                try:
                    row['items'].extend(self._crypto(candidate))
                except Exception as exc:
                    message = f'crypto_ticker:{type(exc).__name__}:{str(exc)[:300]}'
                    row['errors'].append(message)
                    errors.append({'market_id': key, 'error': message})
            cache['markets'][key] = row
            output[key] = row
        self._save_cache(cache)
        return output, {'fresh_fetches': fresh_fetches, 'errors': errors,
                        'markets_with_evidence': sum(bool(v.get('items')) for v in output.values())}


def evidence_payload(candidate, evidence):
    row = (evidence or {}).get(str(candidate.market_id))
    if not row:
        return {'status': 'not_collected', 'items': []}
    return {
        'status': 'available' if row.get('items') else 'unavailable',
        'category_hint': row.get('category_hint'),
        'fetched_at': row.get('fetched_at'),
        'query': row.get('query'),
        'items': (row.get('items') or [])[:20],
        'errors': row.get('errors') or [],
    }

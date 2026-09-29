"""Gemini free-tier client with a hard no-paid-fallback policy.

The caller must use a Google AI Studio / Gemini API project with billing disabled.
The API does not expose project billing status to an API key, so the program requires
an explicit local confirmation flag and then enforces its own daily call ceiling.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
import logging
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .free_config import FREE_GEMINI_MODELS
from .models import utcnow
from .storage import atomic_write


class GeminiFreeError(RuntimeError):
    pass


class FreeQuotaExhausted(GeminiFreeError):
    pass


@dataclass(frozen=True)
class FairValue:
    market_id: str
    fair_yes: Decimal | None
    confidence: Decimal
    source_quality: Decimal
    rationale: str
    skip_reason: str | None


def _bounded_decimal(value, name):
    try:
        d = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f'invalid {name}') from exc
    if not d.is_finite() or not Decimal('0') <= d <= Decimal('1'):
        raise ValueError(f'{name} outside [0,1]')
    return d


def _extract_json(text):
    text = text.strip()
    if text.startswith('```'):
        lines = text.splitlines()
        if lines and lines[0].startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        text = '\n'.join(lines).strip()
    # Be tolerant of a brief model preface while rejecting ambiguous multiple payloads.
    start = text.find('[')
    end = text.rfind(']')
    if start < 0 or end < start:
        raise GeminiFreeError('Gemini response did not contain a JSON array')
    return json.loads(text[start:end + 1])


def parse_fair_values(text, allowed_ids):
    raw = _extract_json(text)
    if not isinstance(raw, list):
        raise GeminiFreeError('Gemini fair-value response must be a list')
    allowed = set(map(str, allowed_ids))
    results = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        mid = str(item.get('market_id', ''))
        if mid not in allowed or mid in results:
            continue
        skip = item.get('skip_reason')
        fair = item.get('fair_yes')
        try:
            fair = None if fair is None else _bounded_decimal(fair, 'fair_yes')
            confidence = _bounded_decimal(item.get('confidence', 0), 'confidence')
            source_quality = _bounded_decimal(item.get('source_quality', 0), 'source_quality')
        except ValueError:
            continue
        rationale = str(item.get('rationale') or '')[:600]
        skip = str(skip)[:240] if skip else None
        if fair is None and not skip:
            skip = 'model_returned_no_probability'
        results[mid] = FairValue(mid, fair, confidence, source_quality, rationale, skip)
    # Missing rows are explicit skips, never silently invented.
    for mid in allowed:
        if mid not in results:
            results[mid] = FairValue(mid, None, Decimal('0'), Decimal('0'), '', 'missing_from_model_response')
    return results


def extract_grounding_sources(response):
    sources = []
    try:
        candidate = response.get('candidates', [])[0]
        metadata = candidate.get('groundingMetadata') or {}
        for chunk in metadata.get('groundingChunks') or []:
            web = chunk.get('web') if isinstance(chunk, dict) else None
            if not isinstance(web, dict):
                continue
            uri, title = web.get('uri'), web.get('title')
            if uri:
                sources.append({'url': str(uri), 'title': str(title or '')[:240]})
        for query in metadata.get('webSearchQueries') or []:
            sources.append({'query': str(query)[:300]})
    except (AttributeError, IndexError, TypeError):
        return []
    # stable de-duplication
    unique = []
    seen = set()
    for source in sources:
        key = json.dumps(source, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(source)
    return unique[:100]


class GeminiFreeClient:
    API = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'

    def __init__(self, api_key, config, state_dir, opener=urlopen):
        self.api_key = api_key.strip() if api_key else ''
        self.config = config
        self.state_dir = Path(state_dir)
        self.quota_path = self.state_dir / 'gemini_free_quota.json'
        self.opener = opener
        self.log = logging.getLogger('paperbot.free.gemini')
        if not config.billing_disabled_confirmed:
            raise GeminiFreeError('Refusing AI calls until GEMINI_BILLING_DISABLED_CONFIRMED=YES')
        if not self.api_key:
            raise GeminiFreeError('GEMINI_API_KEY is required; create a free-tier key with billing disabled')
        if config.gemini_model not in FREE_GEMINI_MODELS:
            raise GeminiFreeError('Primary Gemini model is not in zero-cost allowlist')
        if config.gemini_fallback_model and config.gemini_fallback_model not in FREE_GEMINI_MODELS:
            raise GeminiFreeError('Fallback Gemini model is not in zero-cost allowlist')
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _quota(self):
        today = utcnow().date().isoformat()
        if self.quota_path.exists():
            try:
                data = json.loads(self.quota_path.read_text(encoding='utf-8'))
            except (ValueError, OSError):
                raise GeminiFreeError('Gemini quota ledger corrupt; refusing calls')
            if data.get('date') == today:
                return data
        return {'date': today, 'calls': 0, 'models': {}}

    def _reserve_call(self, model):
        data = self._quota()
        if data['calls'] >= self.config.max_gemini_calls_per_day:
            raise FreeQuotaExhausted('Local zero-cost Gemini daily call cap reached')
        data['calls'] += 1
        data['models'][model] = int(data['models'].get(model, 0)) + 1
        data['updated_at'] = utcnow().isoformat()
        atomic_write(self.quota_path, json.dumps(data, indent=2, sort_keys=True) + '\n')
        return data['calls']

    def _request(self, model, prompt, use_search):
        if model not in FREE_GEMINI_MODELS:
            raise GeminiFreeError('Paid/unverified model blocked')
        if use_search and not FREE_GEMINI_MODELS[model]:
            raise GeminiFreeError('Google Search grounding is not documented as free for this model')
        call_number = self._reserve_call(model)
        body = {
            'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {
                'temperature': float(self.config.ai_temperature),
                'maxOutputTokens': self.config.max_output_tokens,
            },
        }
        if use_search:
            body['tools'] = [{'google_search': {}}]
        req = Request(
            self.API.format(model=model),
            data=json.dumps(body, separators=(',', ':')).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'x-goog-api-key': self.api_key,
                'User-Agent': 'PolymarketPaperBot-Free/1.0',
            },
            method='POST',
        )
        try:
            with self.opener(req, timeout=45) as response:
                raw = json.loads(response.read().decode('utf-8'))
        except HTTPError as exc:
            message = ''
            try:
                message = exc.read().decode('utf-8')[:1000]
            except Exception:
                pass
            if exc.code == 429:
                raise FreeQuotaExhausted('Gemini free-tier quota/rate limit reached; cycle skipped') from exc
            err = GeminiFreeError(f'Gemini HTTP {exc.code} model={model}: {message}')
            err.http_code = exc.code
            raise err from exc
        except (URLError, OSError, TimeoutError, ValueError) as exc:
            raise GeminiFreeError(f'Gemini network/JSON failure model={model}: {exc}') from exc
        try:
            candidate = raw['candidates'][0]
            parts = candidate['content']['parts']
            text = ''.join(str(p.get('text', '')) for p in parts if isinstance(p, dict))
        except (KeyError, IndexError, TypeError) as exc:
            raise GeminiFreeError('Gemini response schema missing candidate text') from exc
        if not text.strip():
            raise GeminiFreeError('Gemini returned empty text')
        self.log.info('Gemini FREE call=%s model=%s search=%s', call_number, model, use_search)
        return text, extract_grounding_sources(raw), model

    def evaluate(self, prompt, market_ids):
        primary = self.config.gemini_model
        use_search = self.config.google_search_grounding
        try:
            text, sources, model = self._request(primary, prompt, use_search)
        except GeminiFreeError as exc:
            code = getattr(exc, 'http_code', None)
            fallback = self.config.gemini_fallback_model
            # Only compatibility/access errors can fall back. Quota errors never trigger more calls.
            if isinstance(exc, FreeQuotaExhausted) or not fallback or code not in (400, 403, 404):
                raise
            self.log.warning('Primary free model unavailable; zero-cost fallback=%s without Search', fallback)
            text, sources, model = self._request(fallback, prompt, False)
        return parse_fair_values(text, market_ids), sources, model

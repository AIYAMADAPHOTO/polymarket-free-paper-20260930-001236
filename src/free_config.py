"""Zero-cost original-post-style strategy configuration.

This module is deliberately separate from the legacy Phase 1-3 configuration so the
previously tested paper-trading stack remains reproducible.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal
import os


FREE_GEMINI_MODELS = {
    # Google currently documents free input/output for these models. Search grounding
    # is free-tier available only on the 2.5 Flash family (subject to Google's quota).
    'gemini-2.5-flash-lite': True,
    'gemini-2.5-flash': True,
    'gemini-3.1-flash-lite': False,
    'gemini-3.5-flash-lite': False,
    'gemini-3.6-flash': False,
    'gemini-3.7-flash': False,
    'gemini-3.8-flash': False,
}


def _bool(env, name, default=False):
    value = env.get(name)
    if value is None:
        return default
    value = str(value).strip().lower()
    if value in ('1', 'true', 'yes', 'on'):
        return True
    if value in ('0', 'false', 'no', 'off'):
        return False
    raise ValueError(f'{name} must be true/false')


def _int(env, name, default, minimum=None, maximum=None):
    raw = env.get(name, str(default))
    value = int(raw)
    if minimum is not None and value < minimum:
        raise ValueError(f'{name} below minimum')
    if maximum is not None and value > maximum:
        raise ValueError(f'{name} above maximum')
    return value


def _dec(env, name, default, minimum=None, maximum=None):
    value = Decimal(str(env.get(name, default)))
    if not value.is_finite():
        raise ValueError(f'{name} must be finite')
    if minimum is not None and value < Decimal(str(minimum)):
        raise ValueError(f'{name} below minimum')
    if maximum is not None and value > Decimal(str(maximum)):
        raise ValueError(f'{name} above maximum')
    return value


@dataclass(frozen=True)
class FreeBotConfig:
    # Source-post adaptation: scan close to 1,000 markets every 10 minutes.
    scan_limit: int = 1000
    page_size: int = 100
    scan_interval_seconds: int = 600
    duration_hours: int = 48
    ai_candidate_limit: int = 40

    # Cheap prefilter before the single Gemini call.
    min_price: Decimal = Decimal('0.03')
    max_price: Decimal = Decimal('0.97')
    min_volume: Decimal = Decimal('1000')
    min_liquidity: Decimal = Decimal('250')
    min_remaining_seconds: int = 3600

    # Source-post trading rules.
    min_edge: Decimal = Decimal('0.08')
    min_confidence: Decimal = Decimal('0.55')
    max_position_fraction: Decimal = Decimal('0.06')
    kelly_multiplier: Decimal = Decimal('1.00')
    max_total_exposure_fraction: Decimal = Decimal('0.30')
    max_positions: int = 5
    min_trade_usd: Decimal = Decimal('0.50')
    daily_loss_fraction: Decimal = Decimal('0.15')
    market_cooldown_seconds: int = 3600

    # Conservative paper exits. Fair-value closure is primary; the others are guards.
    exit_edge_floor: Decimal = Decimal('0.02')
    stop_loss_fraction: Decimal = Decimal('0.15')
    take_profit_fraction: Decimal = Decimal('0.25')
    max_holding_seconds: int = 86400
    resolution_exit_seconds: int = 1800

    # Free Gemini guardrails. No paid fallback exists in this codebase.
    # Current free-tier default for new projects. External freshness is collected
    # by free_evidence.py, so Gemini Search grounding is not required.
    # GitHub Actions runs about 144 cycles/day. Use Flash-Lite here because
    # current free-tier Flash models can have much smaller daily request caps.
    gemini_model: str = 'gemini-3.5-flash-lite'
    gemini_fallback_model: str = ''
    google_search_grounding: bool = False
    max_gemini_calls_per_day: int = 200
    max_output_tokens: int = 8192

    # No-key/no-charge evidence adapter. Cached to avoid hammering public feeds.
    evidence_candidate_limit: int = 8
    evidence_cache_seconds: int = 1800
    evidence_items_per_market: int = 6
    evidence_request_timeout_seconds: int = 12
    ai_temperature: Decimal = Decimal('0.10')

    # This is a user-side assertion because the API does not expose billing status to
    # a simple API key. The program refuses to start AI mode without it.
    billing_disabled_confirmed: bool = False

    def validate(self):
        if not 1 <= self.scan_limit <= 1000:
            raise ValueError('scan_limit must be 1..1000')
        if not 1 <= self.page_size <= 100:
            raise ValueError('page_size must be 1..100')
        if not 60 <= self.scan_interval_seconds <= 3600:
            raise ValueError('scan interval must be 60..3600 seconds')
        if not 1 <= self.duration_hours <= 168:
            raise ValueError('duration_hours must be 1..168')
        if not 1 <= self.ai_candidate_limit <= 100:
            raise ValueError('ai_candidate_limit must be 1..100')
        if not Decimal('0') < self.min_price < self.max_price < Decimal('1'):
            raise ValueError('invalid price bounds')
        if not Decimal('0') < self.min_edge < Decimal('1'):
            raise ValueError('invalid edge threshold')
        if not Decimal('0') < self.max_position_fraction <= Decimal('0.06'):
            raise ValueError('max position fraction cannot exceed 6%')
        if not Decimal('0') < self.max_total_exposure_fraction <= Decimal('1'):
            raise ValueError('invalid exposure fraction')
        if not Decimal('0') <= self.kelly_multiplier <= Decimal('1'):
            raise ValueError('kelly multiplier must be 0..1')
        if self.gemini_model not in FREE_GEMINI_MODELS:
            raise ValueError('Gemini model not in zero-cost allowlist')
        if self.gemini_fallback_model and self.gemini_fallback_model not in FREE_GEMINI_MODELS:
            raise ValueError('Gemini fallback model not in zero-cost allowlist')
        if self.google_search_grounding and not FREE_GEMINI_MODELS[self.gemini_model]:
            raise ValueError('Selected model has no documented free-tier Google Search grounding')
        if not 1 <= self.max_gemini_calls_per_day <= 500:
            raise ValueError('daily Gemini call cap must be 1..500')
        if not 1 <= self.evidence_candidate_limit <= self.ai_candidate_limit:
            raise ValueError('evidence candidate limit must be 1..ai_candidate_limit')
        if not 300 <= self.evidence_cache_seconds <= 86400:
            raise ValueError('evidence cache seconds must be 300..86400')
        if not 1 <= self.evidence_items_per_market <= 20:
            raise ValueError('evidence items per market must be 1..20')
        if not 3 <= self.evidence_request_timeout_seconds <= 60:
            raise ValueError('evidence request timeout must be 3..60')
        return self

    def public_dict(self):
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}


def free_config_from_env(env=None):
    env = os.environ if env is None else env
    if env.get('TRADING_MODE', 'PAPER') != 'PAPER':
        raise ValueError('Zero-cost bot is PAPER only')
    if env.get('STARTING_BALANCE_USD', '50.00') != '50.00':
        raise ValueError('Zero-cost experiment must start at fictional 50.00 USD')
    model = env.get('GEMINI_MODEL', 'gemini-3.5-flash-lite').strip()
    fallback = env.get('GEMINI_FALLBACK_MODEL', '').strip()
    cfg = FreeBotConfig(
        scan_limit=_int(env, 'FREE_SCAN_LIMIT', 1000, 1, 1000),
        page_size=_int(env, 'FREE_PAGE_SIZE', 100, 1, 100),
        scan_interval_seconds=_int(env, 'FREE_SCAN_INTERVAL_SECONDS', 600, 60, 3600),
        duration_hours=_int(env, 'FREEBOT_DURATION_HOURS', 48, 1, 168),
        ai_candidate_limit=_int(env, 'FREE_AI_CANDIDATE_LIMIT', 40, 1, 100),
        min_price=_dec(env, 'FREE_MIN_PRICE', '0.03', 0, 1),
        max_price=_dec(env, 'FREE_MAX_PRICE', '0.97', 0, 1),
        min_volume=_dec(env, 'FREE_MIN_VOLUME', '1000', 0),
        min_liquidity=_dec(env, 'FREE_MIN_LIQUIDITY', '250', 0),
        min_remaining_seconds=_int(env, 'FREE_MIN_REMAINING_SECONDS', 3600, 1),
        min_edge=_dec(env, 'FREE_MIN_EDGE', '0.08', 0, 1),
        min_confidence=_dec(env, 'FREE_MIN_CONFIDENCE', '0.55', 0, 1),
        max_position_fraction=_dec(env, 'FREE_MAX_POSITION_FRACTION', '0.06', 0, '0.06'),
        kelly_multiplier=_dec(env, 'FREE_KELLY_MULTIPLIER', '1.0', 0, 1),
        max_total_exposure_fraction=_dec(env, 'FREE_MAX_EXPOSURE_FRACTION', '0.30', 0, 1),
        max_positions=_int(env, 'FREE_MAX_POSITIONS', 5, 1, 20),
        min_trade_usd=_dec(env, 'FREE_MIN_TRADE_USD', '0.50', 0),
        daily_loss_fraction=_dec(env, 'FREE_DAILY_LOSS_FRACTION', '0.15', 0, 1),
        market_cooldown_seconds=_int(env, 'FREE_MARKET_COOLDOWN_SECONDS', 3600, 1),
        exit_edge_floor=_dec(env, 'FREE_EXIT_EDGE_FLOOR', '0.02', 0, 1),
        stop_loss_fraction=_dec(env, 'FREE_STOP_LOSS_FRACTION', '0.15', 0, 1),
        take_profit_fraction=_dec(env, 'FREE_TAKE_PROFIT_FRACTION', '0.25', 0, 10),
        max_holding_seconds=_int(env, 'FREE_MAX_HOLDING_SECONDS', 86400, 60),
        resolution_exit_seconds=_int(env, 'FREE_RESOLUTION_EXIT_SECONDS', 1800, 1),
        gemini_model=model,
        gemini_fallback_model=fallback,
        google_search_grounding=_bool(env, 'GEMINI_USE_GOOGLE_SEARCH', False),
        max_gemini_calls_per_day=_int(env, 'GEMINI_MAX_CALLS_PER_DAY', 200, 1, 500),
        max_output_tokens=_int(env, 'GEMINI_MAX_OUTPUT_TOKENS', 8192, 256, 32768),
        evidence_candidate_limit=_int(env, 'FREE_EVIDENCE_CANDIDATE_LIMIT', 8, 1, 100),
        evidence_cache_seconds=_int(env, 'FREE_EVIDENCE_CACHE_SECONDS', 1800, 300, 86400),
        evidence_items_per_market=_int(env, 'FREE_EVIDENCE_ITEMS_PER_MARKET', 6, 1, 20),
        evidence_request_timeout_seconds=_int(env, 'FREE_EVIDENCE_TIMEOUT_SECONDS', 12, 3, 60),
        ai_temperature=_dec(env, 'GEMINI_TEMPERATURE', '0.10', 0, 1),
        billing_disabled_confirmed=env.get('GEMINI_BILLING_DISABLED_CONFIRMED', '').strip().upper() == 'YES',
    )
    return cfg.validate()

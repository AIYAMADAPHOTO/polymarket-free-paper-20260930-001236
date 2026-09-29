"""Phase 1 configuration. All balances are fictional USD."""
from decimal import Decimal
from pathlib import Path
from dataclasses import dataclass, asdict
import os

ROOT = Path(__file__).resolve().parent
STARTING_BALANCE_USD = Decimal('50.00')
SCAN_INTERVAL_SECONDS = 45
MAX_MARKETS = 20
PAGE_SIZE = 50
MAX_PAGES = 5
HTTP_TIMEOUT_SECONDS = 15
HTTP_ATTEMPTS = 3
MAX_QUOTE_AGE_SECONDS = 120
GAMMA_URL = 'https://gamma-api.polymarket.com'
CLOB_URL = 'https://clob.polymarket.com'

# All Phase 2 controls are local paper settings, never exchange credentials.
TRADING_MODE = 'PAPER'


def validate_trading_mode():
    if TRADING_MODE != 'PAPER' or os.getenv('TRADING_MODE', 'PAPER') != 'PAPER':
        raise ValueError('Phase 2 supports TRADING_MODE=PAPER only')


@dataclass(frozen=True)
class StrategyConfig:
    min_price: Decimal = Decimal('0.05')
    max_price: Decimal = Decimal('0.95')
    max_spread: Decimal = Decimal('0.02')
    max_relative_spread: Decimal = Decimal('0.10')
    min_volume: Decimal = Decimal('10000')
    min_liquidity: Decimal = Decimal('1000')
    min_depth_usd: Decimal = Decimal('5')
    min_remaining_seconds: int = 3600
    max_data_age_seconds: int = 120
    depth_levels: int = 5
    depth_price_band: Decimal = Decimal('0.03')
    history_seconds: int = 900
    history_min_points: int = 6
    history_min_span_seconds: int = 180
    history_max_gap_seconds: int = 120
    sample_min_seconds: int = 30
    momentum_min_move: Decimal = Decimal('0.015')
    momentum_volume_growth: Decimal = Decimal('0.0001')
    momentum_liquidity_ratio: Decimal = Decimal('0.90')
    reversion_min_shock: Decimal = Decimal('0.025')
    reversion_confirm_move: Decimal = Decimal('0.003')
    imbalance_threshold: Decimal = Decimal('0.65')
    imbalance_persistence_seconds: int = 180
    min_signal_score: Decimal = Decimal('0.70')
    min_combined_score: Decimal = Decimal('0.70')
    weight_momentum: Decimal = Decimal('1')
    weight_mean_reversion: Decimal = Decimal('1')
    weight_imbalance: Decimal = Decimal('1')
    risk_fraction: Decimal = Decimal('0.05')
    max_market_usd: Decimal = Decimal('5')
    max_exposure_fraction: Decimal = Decimal('0.30')
    max_positions: int = 3
    min_entry_usd: Decimal = Decimal('0.50')
    daily_loss_limit_usd: Decimal = Decimal('5')
    consecutive_loss_limit: int = 3
    consecutive_halt_seconds: int = 3600
    cooldown_seconds: int = 900
    cooldown_after_loss_seconds: int = 1800
    take_profit_fraction: Decimal = Decimal('0.05')
    stop_loss_fraction: Decimal = Decimal('0.03')
    time_stop_seconds: int = 3600
    resolution_exit_seconds: int = 1800
    abnormal_spread: Decimal = Decimal('0.04')
    liquidity_exit_ratio: Decimal = Decimal('0.50')
    max_entry_slippage_fraction: Decimal = Decimal('0.02')
    benchmark_budget_usd: Decimal = Decimal('1')

    def validate(self):
        for name, value in asdict(self).items():
            if isinstance(value, Decimal) and (not value.is_finite() or value < 0):
                raise ValueError('Invalid strategy config: ' + name)
            if isinstance(value, int) and value <= 0:
                raise ValueError('Invalid strategy config: ' + name)
        if not 0 < self.min_price < self.max_price < 1:
            raise ValueError('Invalid entry price range')
        if not 0 < self.risk_fraction <= self.max_exposure_fraction <= 1:
            raise ValueError('Invalid risk/exposure fractions')
        if not 0 < self.min_signal_score <= 1 or not 0 < self.min_combined_score <= 1:
            raise ValueError('Invalid signal thresholds')
        if self.weight_momentum + self.weight_mean_reversion + self.weight_imbalance <= 0:
            raise ValueError('Strategy weights must have positive sum')
        if self.min_remaining_seconds <= self.resolution_exit_seconds:
            raise ValueError('Entry horizon must exceed resolution exit horizon')
        if self.history_min_points < 3 or self.history_seconds < self.history_min_span_seconds:
            raise ValueError('Invalid history window')
        return self

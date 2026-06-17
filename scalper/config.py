"""YAML configuration loading and pydantic validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class SessionConfig(BaseModel):
    timezone: str = "America/New_York"
    rth_open: str = "09:30"
    rth_close: str = "16:00"
    flatten_before_close_minutes: int = 5


class TrendConfig(BaseModel):
    ema_fast: int = 9
    ema_slow: int = 21
    ema_trend: int = 50
    vwap_enabled: bool = True
    adx_period: int = 14
    adx_trend_min: float = 22.0
    atr_period: int = 14
    atr_expansion_mult: float = 1.1
    min_trend_score: float = 58.0
    weight_ema: float = 25.0
    weight_vwap: float = 20.0
    weight_adx: float = 25.0
    weight_atr: float = 15.0
    weight_structure: float = 15.0


class L2Config(BaseModel):
    min_l2_score: float = 52.0
    imbalance_threshold: float = 0.55
    depth_levels: int = 5
    min_book_depth: float = 50.0
    spoof_filter_enabled: bool = True
    weight_imbalance: float = 35.0
    weight_depth: float = 25.0
    weight_delta: float = 25.0
    weight_absorption: float = 15.0
    approximation_when_missing: bool = True


class FlowConfig(BaseModel):
    """Circled-move flow gates (CSV bar fields only — no MBO parquet)."""

    min_flow_score: float = 50.0
    flow_strong_score: float = 70.0
    relaxed_min_trend_score: float = 35.0
    min_triggers: int = 2
    short_delta_max: float = -8.0
    long_delta_min: float = 6.0
    strong_delta_magnitude: float = 15.0
    imbalance_threshold: float = 0.55
    book_size_ratio: float = 2.0
    ask_shrink_min_drop: float = 0.15
    depth_spike_ratio: float = 1.25
    weight_delta: float = 40.0
    weight_imbalance: float = 30.0
    weight_book_change: float = 30.0
    use_mbo_new_counts: bool = True
    min_depth_event_rate_per_min: float = 0.0  # 0 = disabled; research ~8k-13k/min


class EntryConfig(BaseModel):
    chop_filter_enabled: bool = True
    chop_adx_max: float = 18.0
    chop_range_atr_mult: float = 0.8
    pullback_to_ema_ticks: int = 3
    max_spread_ticks: int = 4
    require_l2_confirmation: bool = True
    use_flow_signals: bool = False
    # Flow burst: intrabar momentum entries (L2/DOM proxy via orderflow.json — not live MBO parquet).
    flow_burst_mode: bool = False
    flow_burst_poll_sec: float = 15.0
    flow_burst_cooldown_sec: float = 30.0
    orderflow_poll_sec: float = 10.0
    orderflow_max_age_sec: float = 45.0
    entry_cancel_timeout_sec: float = 45.0
    entry_adverse_mid_ticks: int = 9
    pullback_required_for_burst: bool = False
    pullback_mode: bool = True
    min_bars_after_open: int = 0
    cooldown_bars_after_exit: int = 3
    rth_only: bool = False


class ExitConfig(BaseModel):
    stop_loss_ticks: int = 30
    take_profit_ticks: int = 200
    breakeven_enabled: bool = True
    breakeven_trigger_ticks: int = 15
    breakeven_offset_ticks: int = 1
    trailing_enabled: bool = True
    trailing_trigger_ticks: int = 15
    trailing_offset_ticks: int = 5
    max_hold_bars: int = 0  # 0 = disabled
    l2_reversal_exit_enabled: bool = True
    l2_reversal_threshold: float = 35.0
    exit_at_session_end: bool = False


class RiskConfig(BaseModel):
    max_contracts: int = 1
    max_trades_per_session: int = 0  # 0 = disabled
    max_daily_loss_dollars: float = 250.0
    risk_per_trade_dollars: float = 40.0
    max_consecutive_losses: int = 0  # 0 = disabled


class BacktestConfig(BaseModel):
    slippage_ticks: int = 1
    commission_per_side: float = 0.62
    initial_capital: float = 10000.0
    bar_interval_seconds: int = 60


class OptimizeConfig(BaseModel):
    n_trials_default: int = 50
    metric: str = "profit_factor"
    min_trades: int = 3


class ScalperConfig(BaseModel):
    symbol: str
    instrument_family: str = "NQ"
    tick_size: float = 0.25
    tick_value: float = 0.50
    point_value: float = 2.0
    contract_multiplier: float = 2.0
    session: SessionConfig = Field(default_factory=SessionConfig)
    trend: TrendConfig = Field(default_factory=TrendConfig)
    l2: L2Config = Field(default_factory=L2Config)
    flow: FlowConfig = Field(default_factory=FlowConfig)
    entry: EntryConfig = Field(default_factory=EntryConfig)
    exit: ExitConfig = Field(default_factory=ExitConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    optimize: OptimizeConfig = Field(default_factory=OptimizeConfig)


def load_config(path: str | Path) -> ScalperConfig:
    """Load and validate a YAML configuration file."""
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}
    return ScalperConfig.model_validate(raw)


def config_to_dict(config: ScalperConfig) -> dict[str, Any]:
    """Serialize config to a plain dict."""
    return config.model_dump()

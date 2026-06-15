"""Data models for bars, signals, trades, and backtest results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Bias(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class ExitReason(str, Enum):
    STOP = "stop"
    TARGET = "target"
    BREAKEVEN = "breakeven"
    TRAILING = "trailing"
    MAX_TIME = "max_time"
    L2_REVERSAL = "l2_reversal"
    SESSION_END = "session_end"
    MANUAL = "manual"


@dataclass
class Bar:
    """Single OHLCV (+ optional L2) bar."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    bid_size: float | None = None
    ask_size: float | None = None
    bid_depth: float | None = None
    ask_depth: float | None = None
    delta: float | None = None
    index: int = 0


@dataclass
class TrendScore:
    score: float
    bias: Bias
    components: dict[str, float] = field(default_factory=dict)


@dataclass
class L2Score:
    score: float
    components: dict[str, float] = field(default_factory=dict)
    approximated: bool = False


@dataclass
class EntrySignal:
    side: Side
    price: float
    bar_index: int
    trend_score: float
    l2_score: float
    reason: str


@dataclass
class Position:
    side: Side
    entry_price: float
    entry_bar: int
    entry_time: datetime
    quantity: int
    stop_price: float
    target_price: float
    breakeven_active: bool = False
    trailing_active: bool = False
    highest_price: float = 0.0
    lowest_price: float = 0.0


@dataclass
class Trade:
    side: Side
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float
    pnl_ticks: float
    commission: float
    exit_reason: ExitReason
    bars_held: int
    entry_trend_score: float = 0.0
    entry_l2_score: float = 0.0


class BacktestMetrics(BaseModel):
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    total_commission: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    avg_bars_held: float = 0.0


class BacktestResult(BaseModel):
    symbol: str
    config_path: str
    data_path: str
    start_time: str | None = None
    end_time: str | None = None
    bars_processed: int = 0
    trades: list[dict[str, Any]] = Field(default_factory=list)
    metrics: BacktestMetrics = Field(default_factory=BacktestMetrics)
    equity_curve: list[float] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    l2_approximated: bool = False

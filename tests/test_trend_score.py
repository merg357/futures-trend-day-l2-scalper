"""Tests for trend scoring."""

import pandas as pd

from scalper.config import TrendConfig
from scalper.models import Bias
from scalper.trend_score import compute_trend_score, detect_bias


def _bullish_row() -> pd.Series:
    return pd.Series({
        "close": 105.0,
        "open": 104.0,
        "high": 106.0,
        "low": 103.5,
        "ema_fast": 104.0,
        "ema_slow": 102.0,
        "ema_trend": 100.0,
        "vwap": 103.0,
        "adx": 30.0,
        "atr": 2.0,
        "higher_high": 1.0,
        "lower_low": 0.0,
        "volume": 500,
    })


def test_detect_long_bias() -> None:
    assert detect_bias(_bullish_row()) == Bias.LONG


def test_trend_score_range() -> None:
    cfg = TrendConfig()
    result = compute_trend_score(_bullish_row(), 1.8, cfg)
    assert 0 <= result.score <= 100
    assert result.bias == Bias.LONG


def test_none_bias_low_score() -> None:
    row = _bullish_row()
    row["ema_fast"] = 100.0
    row["ema_slow"] = 100.0
    row["close"] = 100.0
    cfg = TrendConfig()
    result = compute_trend_score(row, 2.0, cfg)
    assert result.bias == Bias.NONE
    assert result.score == 0.0

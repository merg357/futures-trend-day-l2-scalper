"""Tests for technical indicators."""

import pandas as pd

from scalper.config import TrendConfig
from scalper.indicators import adx, atr, compute_indicators, ema, vwap


def _sample_df(n: int = 50) -> pd.DataFrame:
    import numpy as np

    rng = np.random.default_rng(1)
    close = 100 + rng.normal(0, 1, n).cumsum()
    return pd.DataFrame({
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": rng.integers(100, 1000, n),
    })


def test_ema_length() -> None:
    df = _sample_df()
    result = ema(df["close"], 9)
    assert len(result) == len(df)


def test_atr_positive() -> None:
    df = _sample_df()
    result = atr(df["high"], df["low"], df["close"], 14)
    assert (result.dropna() >= 0).all()


def test_adx_range() -> None:
    df = _sample_df(80)
    result = adx(df["high"], df["low"], df["close"], 14)
    valid = result.dropna()
    assert (valid >= 0).all()


def test_compute_indicators_adds_columns() -> None:
    df = _sample_df()
    cfg = TrendConfig()
    out = compute_indicators(df, cfg)
    for col in ("ema_fast", "ema_slow", "vwap", "atr", "adx", "delta"):
        assert col in out.columns

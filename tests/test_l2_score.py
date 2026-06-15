"""Tests for L2 scoring."""

import pandas as pd

from scalper.config import L2Config
from scalper.models import Side
from scalper.l2_score import compute_l2_score


def _row_with_l2() -> pd.Series:
    return pd.Series({
        "open": 100.0,
        "high": 101.0,
        "low": 99.5,
        "close": 100.5,
        "volume": 1000,
        "bid_size": 600,
        "ask_size": 400,
        "bid_depth": 3000,
        "ask_depth": 2000,
        "delta": 150,
        "bar_range": 1.5,
        "atr": 2.0,
        "imbalance": 0.2,
    })


def test_l2_score_with_book_data() -> None:
    cfg = L2Config()
    result = compute_l2_score(_row_with_l2(), Side.LONG, cfg)
    assert 0 <= result.score <= 100
    assert not result.approximated


def test_l2_approximation_mode() -> None:
    row = pd.Series({
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.8,
        "volume": 800,
        "atr": 1.5,
        "bar_range": 2.0,
    })
    cfg = L2Config(approximation_when_missing=True)
    result = compute_l2_score(row, Side.LONG, cfg)
    assert result.approximated
    assert result.score > 0

"""Tests for entry and exit rules."""

from datetime import datetime

import pandas as pd

from scalper.config import load_config
from scalper.entry_rules import is_chop
from scalper.exit_rules import evaluate_exit, init_position
from scalper.models import ExitReason, Side

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_chop_detection_low_adx() -> None:
    cfg = load_config(ROOT / "configs" / "mnq_default.yaml")
    row = pd.Series({"adx": 10.0, "atr": 5.0, "bar_range": 4.0, "high": 100, "low": 96})
    assert is_chop(row, cfg.entry) is True


def test_init_position_stop_target() -> None:
    cfg = load_config(ROOT / "configs" / "mnq_default.yaml")
    pos = init_position(Side.LONG, 18500.0, 10, datetime(2024, 6, 3, 10, 0), 1, cfg)
    assert pos.stop_price < pos.entry_price
    assert pos.target_price > pos.entry_price


def test_exit_stop_hit() -> None:
    cfg = load_config(ROOT / "configs" / "mnq_default.yaml")
    pos = init_position(Side.LONG, 18500.0, 10, datetime(2024, 6, 3, 10, 0), 1, cfg)
    bar = pd.Series({
        "open": 18499, "high": 18501, "low": pos.stop_price - 1,
        "close": pos.stop_price, "volume": 500,
        "bid_size": 100, "ask_size": 100, "bid_depth": 500, "ask_depth": 500,
        "delta": 0, "atr": 5, "bar_range": 2,
    })
    price, reason = evaluate_exit(pos, bar, 15, datetime(2024, 6, 3, 10, 5), cfg)
    assert price is not None
    assert reason == ExitReason.STOP

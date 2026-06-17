"""Tests for entry and exit rules."""

from datetime import datetime

import pandas as pd

from scalper.config import load_config
from scalper.entry_rules import evaluate_entry, is_chop, _is_synthetic_flat_bar
from scalper.exit_rules import evaluate_exit, init_position
from scalper.models import Bias, ExitReason, Side
from scalper.session_utils import is_session_end, rth_entry_block_reason
from scalper.trend_score import compute_trend_score

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _production_config():
    return load_config(ROOT / "configs" / "production" / "mnq_walkforward_optimized.yaml")


def test_is_session_end_false_at_2043_et() -> None:
    cfg = _production_config()
    bar_time = datetime(2026, 6, 16, 20, 43)
    assert is_session_end(bar_time, cfg) is False


def test_is_session_end_inactive_when_disabled() -> None:
    cfg = _production_config()
    bar_time = datetime(2026, 6, 16, 15, 57)
    assert cfg.exit.exit_at_session_end is True
    assert is_session_end(bar_time, cfg) is True


def test_rth_entry_not_blocked_outside_rth() -> None:
    cfg = _production_config()
    bar_time = datetime(2026, 6, 16, 20, 43)
    assert cfg.entry.rth_only is False
    assert rth_entry_block_reason(bar_time, cfg) is None


def test_evaluate_entry_not_blocked_by_rth_outside_rth() -> None:
    cfg = _production_config()
    assert cfg.entry.rth_only is False
    assert cfg.entry.min_bars_after_open == 0
    bar_time = datetime(2026, 6, 16, 20, 43)
    assert rth_entry_block_reason(bar_time, cfg) is None


def test_chop_detection_low_adx() -> None:
    cfg = load_config(ROOT / "configs" / "mnq_default.yaml")
    row = pd.Series({"adx": 10.0, "atr": 5.0, "bar_range": 4.0, "high": 100, "low": 96})
    assert is_chop(row, cfg.entry) is True


def test_chop_skips_synthetic_flat_bar() -> None:
    cfg = load_config(ROOT / "configs" / "mnq_default.yaml")
    row = pd.Series({
        "adx": 25.0, "atr": 0.001, "bar_range": 0.0,
        "open": 30519.54, "high": 30519.54, "low": 30519.54, "close": 30519.54,
        "volume": 240,
    })
    assert is_chop(row, cfg.entry) is False


def test_flat_strong_flow_bypasses_trend_bias_none() -> None:
    """Flat OHLC + strong LONG flow burst must enter when trend bias is NONE."""
    from scalper.entry_rules import evaluate_flow_burst_entry

    cfg = _production_config()
    assert cfg.entry.flow_burst_mode is True
    close = 30520.0
    ema_fast = 30521.0
    ema_slow = 30520.0
    row = pd.Series({
        "open": close, "high": close, "low": close, "close": close,
        "volume": 240, "bid": close - 1.0, "ask": close + 0.25,
        "bid_size": 2.0, "ask_size": 3.0, "bid_depth": 30.0, "ask_depth": 100.0,
        "delta": -15.0, "imbalance": -0.9, "atr": 5.0, "bar_range": 0.0,
        "ema_fast": ema_fast, "ema_slow": ema_slow, "ema_trend": 30518.0,
        "vwap": close, "adx": 25.0, "higher_high": 0.0, "lower_low": 1.0,
    })
    prev = row.copy()
    prev["ask_size"] = 8.0
    prev["delta"] = -2.0
    assert _is_synthetic_flat_bar(row)
    trend = compute_trend_score(row, 5.0, cfg.trend)
    assert trend.bias == Bias.NONE
    sig = evaluate_flow_burst_entry(
        row, 5.0, 100, cfg, cooldown_remaining=0, session_bar_index=10,
        bar_time=datetime(2026, 6, 16, 20, 43), prev_row=prev, trend_row=row,
    )
    assert sig is not None
    assert sig.side == Side.SHORT
    assert sig.reason.startswith("flow_burst_short")


def test_ranged_bar_does_not_bypass_trend_bias_none() -> None:
    """Weak flow on ranged bar with NONE trend must not burst-enter."""
    from scalper.entry_rules import evaluate_flow_burst_entry

    cfg = _production_config()
    close = 30520.0
    row = pd.Series({
        "open": close, "high": close + 1.0, "low": close - 1.0, "close": close,
        "volume": 500, "bid": close, "ask": close + 0.25,
        "bid_size": 5.0, "ask_size": 5.0, "bid_depth": 30.0, "ask_depth": 30.0,
        "delta": -3.0, "imbalance": -0.1, "atr": 5.0, "bar_range": 2.0,
        "ema_fast": 30521.0, "ema_slow": 30520.0, "ema_trend": 30525.0,
        "vwap": close, "adx": 25.0, "higher_high": 0.0, "lower_low": 1.0,
    })
    prev = row.copy()
    trend = compute_trend_score(row, 5.0, cfg.trend)
    assert trend.bias == Bias.NONE
    sig = evaluate_flow_burst_entry(
        row, 5.0, 100, cfg, cooldown_remaining=0, session_bar_index=10,
        bar_time=datetime(2026, 6, 16, 20, 43), prev_row=prev, trend_row=row,
    )
    assert sig is None


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


def _flat_bar_series(close: float, bid: float, ask: float) -> pd.Series:
    return pd.Series({
        "open": close, "high": close, "low": close, "close": close,
        "volume": 240, "bid": bid, "ask": ask,
        "bid_size": 100, "ask_size": 100, "bid_depth": 500, "ask_depth": 500,
        "delta": 0, "atr": 5, "bar_range": 0.0,
    })


def test_flat_bar_short_updates_lowest_price_from_bid() -> None:
    """Synthetic flat OHLC must use bid for favorable SHORT excursion."""
    cfg = _production_config()
    entry = 30519.29
    close = 30519.54
    bid = entry - 4 * cfg.tick_size  # 4 ticks favorable — below BE trigger (15 ticks)
    pos = init_position(Side.SHORT, entry, 10, datetime(2026, 6, 16, 20, 43), 1, cfg)
    bar = _flat_bar_series(close, bid, close + 0.13)

    price, reason = evaluate_exit(pos, bar, 11, datetime(2026, 6, 16, 20, 44), cfg)

    assert price is None
    assert reason is None
    assert pos.lowest_price == bid
    assert pos.breakeven_active is False


def test_flat_bar_short_triggers_breakeven_when_bid_16_ticks_below_entry() -> None:
    """Bid 16 ticks below entry on flat bar should arm breakeven (not trail) for SHORT."""
    cfg = _production_config()
    entry = 30519.29
    bid = entry - 16 * cfg.tick_size
    ask = bid + cfg.tick_size
    pos = init_position(Side.SHORT, entry, 10, datetime(2026, 6, 16, 20, 43), 1, cfg)
    bar = _flat_bar_series(bid, bid, ask)

    price, reason = evaluate_exit(pos, bar, 11, datetime(2026, 6, 16, 20, 44), cfg)

    assert pos.lowest_price == bid
    assert pos.breakeven_active is True
    assert pos.trailing_active is False
    assert price is None
    assert reason is None


def test_flat_bar_short_trailing_at_20_ticks_favorable() -> None:
    """Bid 20 ticks below entry arms trailing on flat bar (retuned trigger)."""
    cfg = _production_config()
    entry = 30519.29
    close = 30519.54
    bid = entry - cfg.exit.trailing_trigger_ticks * cfg.tick_size
    pos = init_position(Side.SHORT, entry, 10, datetime(2026, 6, 16, 20, 43), 1, cfg)
    bar = _flat_bar_series(close, bid, close + 0.13)

    price, reason = evaluate_exit(pos, bar, 11, datetime(2026, 6, 16, 20, 44), cfg)

    assert pos.breakeven_active is True
    assert pos.trailing_active is True
    expected_stop = bid + cfg.exit.trailing_offset_ticks * cfg.tick_size
    assert pos.stop_price == expected_stop
    assert price == expected_stop
    assert reason == ExitReason.TRAILING


def test_flat_bar_short_breakeven_at_exactly_10_ticks_favorable() -> None:
    """Bid exactly 10 ticks below entry arms BE on flat bar (retuned trigger)."""
    cfg = _production_config()
    entry = 30519.29
    bid = entry - cfg.exit.breakeven_trigger_ticks * cfg.tick_size
    ask = bid + cfg.tick_size
    pos = init_position(Side.SHORT, entry, 10, datetime(2026, 6, 16, 20, 43), 1, cfg)
    bar = _flat_bar_series(bid, bid, ask)

    price, reason = evaluate_exit(pos, bar, 11, datetime(2026, 6, 16, 20, 44), cfg)

    assert pos.breakeven_active is True
    assert pos.trailing_active is False
    assert price is None
    assert reason is None


def test_normal_bar_exit_unchanged_without_bid_ask_merge() -> None:
    """Bars with real range must not alter high/low from bid/ask."""
    cfg = _production_config()
    entry = 30519.29
    pos = init_position(Side.SHORT, entry, 10, datetime(2026, 6, 16, 20, 43), 1, cfg)
    bar = pd.Series({
        "open": entry, "high": entry + 0.5, "low": entry - 0.5, "close": entry,
        "volume": 500, "bid": entry - 2.25, "ask": entry + 0.25,
        "bid_size": 100, "ask_size": 100, "bid_depth": 500, "ask_depth": 500,
        "delta": 0, "atr": 5, "bar_range": 1.0,
    })

    evaluate_exit(pos, bar, 11, datetime(2026, 6, 16, 20, 44), cfg)

    assert pos.lowest_price == entry - 0.5
    assert pos.highest_price == entry + 0.5


def test_max_hold_exits_at_five_bars() -> None:
    """max_hold_bars=5 must force MAX_TIME exit."""
    cfg = _production_config()
    assert cfg.exit.max_hold_bars == 5
    entry = 30519.29
    pos = init_position(Side.LONG, entry, 0, datetime(2026, 6, 16, 10, 0), 1, cfg)
    bar = pd.Series({
        "open": entry, "high": entry + 0.25, "low": entry - 0.25, "close": entry,
        "volume": 500, "bid_size": 100, "ask_size": 100,
        "bid_depth": 500, "ask_depth": 500, "delta": 0, "atr": 5, "bar_range": 0.5,
    })
    price, reason = evaluate_exit(pos, bar, 5, datetime(2026, 6, 16, 10, 5), cfg)
    assert reason == ExitReason.MAX_TIME
    assert price == entry

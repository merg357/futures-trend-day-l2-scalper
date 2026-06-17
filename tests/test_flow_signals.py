"""Tests for circled-move flow signals."""

import pandas as pd

from scalper.config import FlowConfig
from scalper.flow_signals import (
    build_intrabar_snapshot_row,
    compute_flow_for_side,
    compute_flow_signal,
    flow_burst_passes,
    flow_supports_side,
)
from scalper.models import Side


def _bar(**kwargs: float) -> pd.Series:
    base = {
        "open": 30500.0,
        "high": 30501.0,
        "low": 30499.0,
        "close": 30500.5,
        "volume": 500,
        "bid_size": 3.0,
        "ask_size": 3.0,
        "bid_depth": 30.0,
        "ask_depth": 30.0,
        "delta": 0.0,
        "imbalance": 0.0,
    }
    base.update(kwargs)
    return pd.Series(base)


def test_long_rally_delta_and_depth_burst() -> None:
    """Evening rally open: delta +19/+20 with depth churn (research move 1)."""
    cfg = FlowConfig()
    prev = _bar(bid_depth=40.0, ask_depth=40.0, bid_size=4.0, ask_size=4.0)
    row = _bar(delta=20.0, bid_depth=120.0, ask_depth=100.0, bid_size=8.0, ask_size=6.0)
    flow = compute_flow_for_side(row, prev, Side.LONG, cfg)
    assert flow.triggers["delta"] is True
    assert flow.triggers["depth_burst"] is True
    assert flow.triggers_hit >= 2
    assert flow.side == Side.LONG
    assert flow.score >= cfg.min_flow_score
    assert flow_supports_side(flow, Side.LONG, cfg)


def test_short_drop_delta_imbalance_shrink() -> None:
    """Morning drop: delta -20, ask-heavy book, ask_size falling (research move 2)."""
    cfg = FlowConfig()
    prev = _bar(ask_size=10.0, bid_size=5.0, imbalance=-0.5, delta=-2.0)
    row = _bar(
        delta=-20.0,
        ask_size=3.0,
        bid_size=2.0,
        imbalance=-1.0,
    )
    flow = compute_flow_for_side(row, prev, Side.SHORT, cfg)
    assert flow.triggers["delta"] is True
    assert flow.triggers["imbalance"] is True
    assert flow.triggers["ask_shrink"] is True
    assert flow.triggers_hit == 3
    assert flow.side == Side.SHORT
    assert flow_supports_side(flow, Side.SHORT, cfg)


def test_single_trigger_does_not_pass() -> None:
    cfg = FlowConfig()
    row = _bar(delta=3.0, imbalance=0.1)
    flow = compute_flow_for_side(row, None, Side.LONG, cfg)
    assert flow.triggers_hit == 0
    assert flow.side is None
    assert not flow_supports_side(flow, Side.LONG, cfg)


def test_compute_flow_signal_picks_stronger_side() -> None:
    cfg = FlowConfig()
    row = _bar(delta=-12.0, imbalance=-0.8, ask_size=2.0, bid_size=8.0)
    prev = _bar(ask_size=6.0, bid_size=4.0)
    flow = compute_flow_signal(row, prev, cfg)
    assert flow.side == Side.SHORT
    assert flow.score >= cfg.min_flow_score


def test_flow_burst_skips_pullback() -> None:
    """Strong flow burst must enter without EMA pullback touch (research: not pullbacks)."""
    from scalper.config import ScalperConfig
    from scalper.entry_rules import evaluate_flow_burst_entry

    cfg = FlowConfig()
    raw = ScalperConfig(symbol="MNQ").model_dump()
    raw["entry"]["flow_burst_mode"] = True
    raw["entry"]["use_flow_signals"] = True
    raw["entry"]["pullback_required_for_burst"] = False
    raw["entry"]["pullback_mode"] = False
    raw["flow"] = cfg.model_dump()
    config = ScalperConfig.model_validate(raw)

    close = 30520.0
    ema_fast = 30525.0  # price below EMA — no pullback touch for LONG
    row = pd.Series({
        "open": close, "high": close, "low": close, "close": close,
        "volume": 240, "bid": close - 0.25, "ask": close + 0.25,
        "bid_size": 12.0, "ask_size": 3.0, "bid_depth": 120.0, "ask_depth": 40.0,
        "delta": 20.0, "imbalance": 0.6, "atr": 5.0, "bar_range": 0.0,
        "ema_fast": ema_fast, "ema_slow": close, "ema_trend": close - 5,
        "vwap": close, "adx": 25.0, "higher_high": 1.0, "lower_low": 0.0,
    })
    prev = _bar(bid_depth=40.0, ask_depth=40.0, bid_size=4.0, ask_size=4.0)
    sig = evaluate_flow_burst_entry(
        row, 5.0, 100, config, 0, 10,
        __import__("datetime").datetime(2026, 6, 16, 20, 0),
        prev_row=prev, trend_row=row,
    )
    assert sig is not None
    assert sig.side == Side.LONG
    assert sig.reason.startswith("flow_burst_long")


def test_flow_burst_passes_requires_score_and_triggers() -> None:
    cfg = FlowConfig()
    row = _bar(delta=3.0)
    flow = compute_flow_for_side(row, None, Side.LONG, cfg)
    assert not flow_burst_passes(flow, Side.LONG, cfg)


def test_mbo_new_counts_short_imbalance() -> None:
    """MBO ask/bid new skew replaces L0 book_size_ratio when counters present."""
    cfg = FlowConfig(book_size_ratio=2.0)
    row = _bar(
        bid_size=10.0,
        ask_size=10.0,
        mbo_bid_new_count=2.0,
        mbo_ask_new_count=8.0,
        imbalance=0.0,
    )
    flow = compute_flow_for_side(row, None, Side.SHORT, cfg)
    assert flow.triggers["imbalance"] is True


def test_mbo_new_counts_long_imbalance() -> None:
    cfg = FlowConfig(book_size_ratio=2.0)
    row = _bar(
        bid_size=10.0,
        ask_size=10.0,
        mbo_bid_new_count=10.0,
        mbo_ask_new_count=2.0,
        imbalance=0.0,
    )
    flow = compute_flow_for_side(row, None, Side.LONG, cfg)
    assert flow.triggers["imbalance"] is True


def test_build_intrabar_includes_mbo_and_depth_fields(tmp_path) -> None:
    import json

    of_path = tmp_path / "orderflow.json"
    of_path.write_text(
        json.dumps({
            "MNQ": {
                "obi": 0.42,
                "tape_ratio": 1.8,
                "cvd": 120.0,
                "cvd_recent": 15.0,
                "bid_levels": [{"price": 30500.0, "qty": 5}],
                "ask_levels": [{"price": 30500.25, "qty": 3}],
                "total_bid_vol": 50,
                "total_ask_vol": 30,
                "spread": 0.25,
                "mid_price": 30500.125,
                "mbo_features": {
                    "mbo_bid_new_count": 4,
                    "mbo_ask_new_count": 12,
                    "mbo_event_count": 200,
                },
                "depth_event_count_60s": 9000,
                "depth_event_rate_per_min": 9000.0,
                "aggressive_buy_volume_60s": 40.0,
                "aggressive_sell_volume_60s": 60.0,
                "net_aggressive_volume_60s": -20.0,
            }
        }),
        encoding="utf-8",
    )
    minute = _bar()
    snap, _ = build_intrabar_snapshot_row(
        "MNQ",
        minute_bar_row=minute,
        cvd_at_minute_open=100.0,
        orderflow_path=of_path,
    )
    assert snap is not None
    assert float(snap["obi"]) == 0.42
    assert float(snap["tape_ratio"]) == 1.8
    assert float(snap["mbo_ask_new_count"]) == 12.0
    assert float(snap["depth_event_rate_per_min"]) == 9000.0
    assert float(snap["net_aggressive_volume_60s"]) == -20.0

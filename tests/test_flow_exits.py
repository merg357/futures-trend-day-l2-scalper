"""Tests for intrabar flow exits, adverse entry cancel, and retuned exit stack."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd

from scalper.config import load_config
from scalper.exit_rules import (
    evaluate_exit,
    evaluate_flow_exit,
    flow_exit_delta_flip,
    flow_exit_mbo_reversal,
    init_position,
)
from scalper.models import ExitReason, Side
from scalper.paper_runner import PendingEntry, RunnerState, _maybe_poll_adverse_entry_cancel

ROOT = Path(__file__).resolve().parents[1]


def _production_config():
    return load_config(ROOT / "configs" / "production" / "mnq_walkforward_optimized.yaml")


def _snap(**kwargs: float) -> pd.Series:
    base = {
        "open": 30500.0,
        "high": 30500.0,
        "low": 30500.0,
        "close": 30500.0,
        "volume": 240,
        "delta": 0.0,
        "mbo_bid_new_count": 5.0,
        "mbo_ask_new_count": 5.0,
    }
    base.update(kwargs)
    return pd.Series(base)


def test_production_exit_stack_retuned() -> None:
    cfg = _production_config()
    assert cfg.exit.stop_loss_ticks == 30
    assert cfg.exit.take_profit_ticks == 50
    assert cfg.exit.breakeven_trigger_ticks == 10
    assert cfg.exit.trailing_trigger_ticks == 20
    assert cfg.exit.trailing_offset_ticks == 4
    assert cfg.exit.max_hold_bars == 5
    assert cfg.exit.exit_at_session_end is True
    assert cfg.exit.l2_reversal_exit_enabled is False


def test_l2_reversal_disabled_in_bar_exit() -> None:
    cfg = _production_config()
    entry = 30500.0
    pos = init_position(Side.LONG, entry, 0, datetime(2026, 6, 17, 10, 0), 1, cfg)
    bar = pd.Series({
        "open": entry, "high": entry + 0.25, "low": entry - 0.25, "close": entry,
        "volume": 500, "bid_size": 2.0, "ask_size": 20.0,
        "bid_depth": 10.0, "ask_depth": 200.0, "delta": -50.0,
        "atr": 5.0, "bar_range": 0.5,
    })
    price, reason = evaluate_exit(pos, bar, 3, datetime(2026, 6, 17, 10, 3), cfg)
    assert reason != ExitReason.L2_REVERSAL
    assert price is None


def test_flow_exit_delta_flip_long() -> None:
    cfg = _production_config()
    pos = init_position(Side.LONG, 30500.0, 0, datetime(2026, 6, 17, 10, 0), 1, cfg)
    snap = _snap(delta=-10.0)
    assert flow_exit_delta_flip(snap, Side.LONG, cfg.flow)
    price, reason = evaluate_flow_exit(pos, snap, cfg)
    assert reason == ExitReason.FLOW_DELTA_FLIP
    assert price == 30500.0


def test_flow_exit_delta_flip_short() -> None:
    cfg = _production_config()
    pos = init_position(Side.SHORT, 30500.0, 0, datetime(2026, 6, 17, 10, 0), 1, cfg)
    snap = _snap(delta=8.0, close=30500.0)
    assert flow_exit_delta_flip(snap, Side.SHORT, cfg.flow)
    price, reason = evaluate_flow_exit(pos, snap, cfg)
    assert reason == ExitReason.FLOW_DELTA_FLIP


def test_flow_exit_mbo_reversal_long() -> None:
    cfg = _production_config()
    pos = init_position(Side.LONG, 30500.0, 0, datetime(2026, 6, 17, 10, 0), 1, cfg)
    snap = _snap(delta=2.0, mbo_bid_new_count=2.0, mbo_ask_new_count=8.0)
    assert flow_exit_mbo_reversal(snap, Side.LONG, cfg.flow)
    price, reason = evaluate_flow_exit(pos, snap, cfg)
    assert reason == ExitReason.FLOW_MBO_REVERSAL


def test_flow_exit_mbo_reversal_short() -> None:
    cfg = _production_config()
    pos = init_position(Side.SHORT, 30500.0, 0, datetime(2026, 6, 17, 10, 0), 1, cfg)
    snap = _snap(delta=-2.0, mbo_bid_new_count=10.0, mbo_ask_new_count=2.0)
    assert flow_exit_mbo_reversal(snap, Side.SHORT, cfg.flow)
    price, reason = evaluate_flow_exit(pos, snap, cfg)
    assert reason == ExitReason.FLOW_MBO_REVERSAL


def test_flow_exit_trail_intrabar_long() -> None:
    cfg = _production_config()
    entry = 30500.0
    pos = init_position(Side.LONG, entry, 0, datetime(2026, 6, 17, 10, 0), 1, cfg)
    favorable_mid = entry + cfg.exit.trailing_trigger_ticks * cfg.tick_size + 1.0
    snap_fav = _snap(delta=5.0, close=favorable_mid)
    evaluate_flow_exit(pos, snap_fav, cfg)
    assert pos.trailing_active is True

    trail_stop = pos.stop_price
    snap_exit = _snap(delta=5.0, close=trail_stop - cfg.tick_size)
    price, reason = evaluate_flow_exit(pos, snap_exit, cfg)
    assert reason == ExitReason.FLOW_TRAIL_INTRABAR
    assert price == trail_stop - cfg.tick_size


def test_adverse_cancel_timeout(monkeypatch, tmp_path) -> None:
    cfg = _production_config()
    cfg.entry.entry_cancel_timeout_sec = 0.01
    state = RunnerState(
        pending_entry=PendingEntry(
            order_id="L2SCALP_ENT_test_1",
            submit_ts=time.monotonic() - 1.0,
            side=Side.LONG,
            limit_price=30500.0,
            quantity=1,
        ),
    )
    row = pd.Series({"timestamp": "2026-06-17 10:00:00", "close": 30500.0})
    gateway = MagicMock()
    gateway.cancel_order.return_value = {"status": "cancelled"}

    of_path = tmp_path / "orderflow.json"
    of_path.write_text(
        '{"MNQ": {"cvd": 100, "mid_price": 30500, "bid_levels": [{"price": 30499.75, "qty": 5}], '
        '"ask_levels": [{"price": 30500, "qty": 5}], "spread": 0.25}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("FUTURESBOT_ROOT", str(tmp_path.parent))
    monkeypatch.setattr(
        "scalper.flow_signals.ORDERFLOW_PATH",
        of_path,
    )

    _maybe_poll_adverse_entry_cancel(row, state, cfg, gateway)
    gateway.cancel_order.assert_called_once()
    assert state.pending_entry is None

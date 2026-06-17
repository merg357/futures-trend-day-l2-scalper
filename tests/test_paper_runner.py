"""Tests for paper_runner follow-mode startup and phantom-trade fixes."""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd

from scalper.config import load_config
from scalper.exit_rules import init_position
from scalper.live_gateway import is_entry_blocked
from scalper.models import Side
from scalper.paper_runner import (
    RunnerState,
    TradeLogDeduper,
    _already_entered_this_minute,
    _clear_position_if_gateway_flat,
    _flow_burst_cooldown_active,
    _init_follow_session_state,
    _mark_flow_burst_entry,
    _seed_follow_processed_bars,
)

ROOT = Path(__file__).resolve().parents[1]


def test_seed_follow_leaves_last_bar_eligible() -> None:
    df = pd.DataFrame(
        {
            "timestamp": ["2026-06-16 20:34:00", "2026-06-16 20:35:00", "2026-06-16 20:36:00"],
            "close": [100.0, 101.0, 102.0],
        }
    )
    state = RunnerState(processed_timestamps=set())
    last_ts = _seed_follow_processed_bars(df, state)

    assert last_ts == "2026-06-16 20:36:00"
    assert "2026-06-16 20:34:00" in state.processed_timestamps
    assert "2026-06-16 20:35:00" in state.processed_timestamps
    assert "2026-06-16 20:36:00" not in state.processed_timestamps


def test_seed_follow_single_bar_leaves_it_eligible() -> None:
    df = pd.DataFrame({"timestamp": ["2026-06-16 20:36:00"], "close": [102.0]})
    state = RunnerState(processed_timestamps=set())
    last_ts = _seed_follow_processed_bars(df, state)

    assert last_ts == "2026-06-16 20:36:00"
    assert len(state.processed_timestamps) == 0


def test_init_follow_session_state_after_restart() -> None:
    """Restart must not reset session_bar to 0 when CSV already has many same-day bars."""
    timestamps = [f"2026-06-16 09:3{i}:00" for i in range(10)] + [
        f"2026-06-16 10:{i:02d}:00" for i in range(10)
    ]
    df = pd.DataFrame({"timestamp": timestamps, "close": [100.0] * len(timestamps)})
    state = RunnerState(processed_timestamps=set())
    _seed_follow_processed_bars(df, state)

    processed = _init_follow_session_state(df, state)

    assert processed == len(timestamps) - 1
    assert state.session_bar == len(timestamps) - 1
    assert state.prev_session_date == pd.Timestamp("2026-06-16").date()
    # session_bar tracks bars for optional min_bars_after_open (0 = no warmup gate).
    assert state.session_bar + 1 >= 1


def test_init_follow_session_state_resets_on_new_session_date() -> None:
    df = pd.DataFrame(
        {
            "timestamp": ["2026-06-15 15:59:00", "2026-06-16 09:30:00", "2026-06-16 09:31:00"],
            "close": [100.0, 101.0, 102.0],
        }
    )
    state = RunnerState(processed_timestamps=set())
    _seed_follow_processed_bars(df, state)
    processed = _init_follow_session_state(df, state)

    assert processed == 1
    assert state.session_bar == 1
    assert state.prev_session_date == pd.Timestamp("2026-06-16").date()


def test_is_entry_blocked_fill_divergence() -> None:
    assert is_entry_blocked({"status": "blocked_fill_divergence"}) is True
    assert is_entry_blocked({"status": "blocked_paper_mode"}) is False
    assert is_entry_blocked({"status": "submitted"}) is False
    assert is_entry_blocked({"status": "nt8_rejected"}) is True


def test_trade_log_deduper_skips_identical_round_trip(tmp_path: Path) -> None:
    trades_path = tmp_path / "trades.jsonl"
    deduper = TradeLogDeduper(trades_path)
    record = {
        "entry_time": "2026-06-16 10:00:00",
        "exit_time": "2026-06-16 10:05:00",
        "side": "LONG",
        "entry_price": 18500.0,
        "exit_reason": "stop",
    }
    assert deduper.append(record) is True
    assert deduper.append(dict(record)) is False
    assert len(trades_path.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_trade_log_deduper_skips_same_bar_entry(tmp_path: Path) -> None:
    trades_path = tmp_path / "trades.jsonl"
    deduper = TradeLogDeduper(trades_path)
    first = {
        "entry_time": "2026-06-16 10:00:00",
        "exit_time": "2026-06-16 10:05:00",
        "side": "LONG",
        "entry_price": 18500.0,
        "exit_reason": "stop",
    }
    second = {
        "entry_time": "2026-06-16 10:00:00",
        "exit_time": "2026-06-16 10:07:00",
        "side": "LONG",
        "entry_price": 18500.0,
        "exit_reason": "trailing",
    }
    assert deduper.append(first) is True
    assert deduper.append(second) is False


def test_clear_position_if_gateway_flat() -> None:
    cfg = load_config(str(ROOT / "configs" / "production" / "mnq_walkforward_optimized.yaml"))
    state = RunnerState(
        position=init_position(
            Side.LONG, 18500.0, 1, datetime(2026, 6, 16, 10, 0), 1, cfg
        )
    )
    gateway = MagicMock()
    gateway.query_market_position.return_value = 0
    _clear_position_if_gateway_flat(state, gateway)
    assert state.position is None


def test_intrabar_not_double_entry_same_minute() -> None:
    """Flow burst must not re-enter within the same minute after first burst."""
    cfg = load_config(str(ROOT / "configs" / "production" / "mnq_walkforward_optimized.yaml"))
    state = RunnerState()
    row = pd.Series({"timestamp": "2026-06-16 20:43:00", "close": 30520.0})
    assert _already_entered_this_minute(state, row) is False
    _mark_flow_burst_entry(state, row)
    assert _already_entered_this_minute(state, row) is True
    assert state.flow_burst_entry_minute == "2026-06-16 20:43"
    later = pd.Series({"timestamp": "2026-06-16 20:43:30", "close": 30521.0})
    assert _already_entered_this_minute(state, later) is True
    next_min = pd.Series({"timestamp": "2026-06-16 20:44:00", "close": 30522.0})
    assert _already_entered_this_minute(state, next_min) is False


def test_flow_burst_cooldown_blocks_rapid_reentry() -> None:
    cfg = load_config(str(ROOT / "configs" / "production" / "mnq_walkforward_optimized.yaml"))
    state = RunnerState()
    row = pd.Series({"timestamp": "2026-06-16 20:43:00", "close": 30520.0})
    _mark_flow_burst_entry(state, row)
    assert _flow_burst_cooldown_active(state, cfg) is True

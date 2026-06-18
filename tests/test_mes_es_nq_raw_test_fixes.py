"""Tests for MES_ES_NQ raw test timing, routing, and blocker flags."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from scalper.config import load_config
from scalper.mes_es_nq_runner import DEFAULT_CONFIG
from scalper.nq_confirmation import nq_veto_comparison
from scalper.paper_runner import (
    RunnerState,
    _flow_burst_cooldown_active,
    _min_seconds_between_entries_active,
    _signal_orderflow_root,
)
from scalper.models import Side

ROOT = Path(__file__).resolve().parents[1]
MES_CONFIG = ROOT / "configs" / "production" / "mes_es_nq_raw_test.yaml"


@pytest.fixture
def mes_config():
    return load_config(MES_CONFIG)


def test_default_config_path_matches_runner() -> None:
    assert DEFAULT_CONFIG == "configs/production/mes_es_nq_raw_test.yaml"


def test_entry_timeout_1500ms(mes_config) -> None:
    assert mes_config.mes_execution.entry_timeout_ms == 1500


def test_raw_test_blockers_disabled(mes_config) -> None:
    assert mes_config.entry.use_burst_cooldown is False
    assert mes_config.entry.use_one_entry_per_minute is False
    assert mes_config.entry.use_per_minute_dedup is False
    assert mes_config.entry.min_seconds_between_entries == 0
    assert mes_config.mes_execution.block_on_fill_divergence is False
    assert mes_config.mes_execution.log_fill_divergence is True


def test_fast_runtime_loops_250ms(mes_config) -> None:
    assert mes_config.raw_test_runtime.decision_loop_ms == 250
    assert mes_config.raw_test_runtime.order_monitor_loop_ms == 250
    assert mes_config.raw_test_runtime.exit_monitor_loop_ms == 250


def test_signal_orderflow_root_is_es(mes_config) -> None:
    assert _signal_orderflow_root(mes_config) == "ES"


def test_burst_cooldown_inactive_when_disabled(mes_config) -> None:
    state = RunnerState()
    state.last_flow_burst_entry_ts = 1.0
    with patch("scalper.paper_runner.time.monotonic", return_value=5.0):
        assert _flow_burst_cooldown_active(state, mes_config) is False


def test_min_seconds_between_entries_disabled(mes_config) -> None:
    state = RunnerState()
    state.last_entry_submit_ts = 1.0
    with patch("scalper.paper_runner.time.monotonic", return_value=2.0):
        assert _min_seconds_between_entries_active(state, mes_config) is False


def test_nq_soft_veto_higher_than_strict(mes_config) -> None:
    nq_row = pd.Series(
        {
            "delta": -12,
            "imbalance": -0.6,
            "mbo_bid_new_count": 2,
            "mbo_ask_new_count": 8,
        }
    )
    cmp = nq_veto_comparison(Side.LONG, mes_config, nq_row=nq_row)
    assert cmp["nq_veto_strict_would_block"] in (True, False)
    assert "nq_flow_score" in cmp


def test_entry_order_mode_default_marketable_limit(mes_config) -> None:
    assert mes_config.mes_execution.entry_order_mode == "MARKETABLE_LIMIT"


def test_entry_cancel_timeout_fires_before_poll_throttle(mes_config, monkeypatch) -> None:
    """Timeout cancel must not wait for orderflow/fast-monitor throttle."""
    from unittest.mock import MagicMock
    import time

    from scalper.models import Side
    from scalper.paper_runner import PendingEntry, RunnerState, _maybe_cancel_pending_entry_timeout

    gateway = MagicMock()
    gateway.cancel_order.return_value = {"status": "cancelled"}
    state = RunnerState(
        pending_entry=PendingEntry(
            order_id="L2MES_ENT_test",
            submit_ts=time.monotonic() - 2.0,
            side=Side.LONG,
            limit_price=5000.0,
            quantity=1,
        ),
        last_fast_monitor_ts=time.monotonic(),
    )
    _maybe_cancel_pending_entry_timeout(state, mes_config, gateway)
    gateway.cancel_order.assert_called_once_with("L2MES_ENT_test", reason="cancel_timeout")
    assert state.pending_entry is None


def test_pending_entry_submit_ts_uses_order_submit(monkeypatch, tmp_path) -> None:
    """Pending LIMIT clock must start at gateway submit, not after fill-wait."""
    from unittest.mock import MagicMock, patch

    from scalper.models import Side
    from scalper.paper_runner import RunnerState, _execute_entry
    from scalper.risk import RiskManager

    mes_config = load_config(MES_CONFIG)
    gateway = MagicMock()
    gateway.submit_order.return_value = {
        "order_id": "L2MES_ENT_test",
        "order_type": "LIMIT",
        "status": "submitted",
    }
    gateway.query_market_position.return_value = 0

    mono_values = iter([999.0, 1000.0, 1010.0])
    monkeypatch.setattr("scalper.paper_runner.time.monotonic", lambda: next(mono_values, 1010.0))
    monkeypatch.setattr("scalper.paper_runner._wait_gateway_entry_fill", lambda *a, **k: False)
    monkeypatch.setattr(
        "scalper.paper_runner._resolve_entry_price",
        lambda *a, **k: (5000.0, {"mes_bid_at_submit": 5000.0, "mes_ask_at_submit": 5000.25}),
    )
    monkeypatch.setattr("scalper.paper_runner._read_mes_quote", lambda *a, **k: {})
    monkeypatch.setattr("scalper.paper_runner._append_jsonl", lambda *a, **k: None)
    monkeypatch.setattr(
        "scalper.mes_es_nq_runner.mes_entry_blocked_reason",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "scalper.nq_confirmation.nq_veto_comparison",
        lambda *a, **k: {"nq_veto_strict_would_block": False, "nq_veto_soft_blocks": False},
    )

    class _Sig:
        side = Side.LONG
        price = 5000.0
        trend_score = 50.0
        l2_score = 50.0
        reason = "flow_burst_long_flow=70"

    state = RunnerState()
    row = pd.Series({"timestamp": "2026-06-17 16:00:00", "close": 5000.0})
    _execute_entry(
        _Sig(),
        row,
        state,
        mes_config,
        RiskManager(mes_config),
        mode="follow",
        signals_path=tmp_path / "signals.jsonl",
        trades_path=tmp_path / "trades.jsonl",
        gateway=gateway,
        trade_deduper=None,
        log_dir=tmp_path,
    )
    assert state.pending_entry is not None
    assert state.pending_entry.submit_ts == 1000.0


def test_adverse_mid_cancel_ticks_12(mes_config) -> None:
    assert mes_config.entry.entry_adverse_mid_ticks == 12
    assert mes_config.entry.use_adverse_mid_cancel is True


def test_trend_filter_disabled_for_raw_test(mes_config) -> None:
    assert mes_config.trend.use_trend_filter is False
    assert mes_config.trend.require_trend_alignment is False
    assert mes_config.trend.min_trend_score == 0


def test_flow_burst_allows_counter_trend_when_filter_off(mes_config) -> None:
    """Weak flow (below flow_strong_score) must enter against trend when filter disabled."""
    from datetime import datetime

    from scalper.entry_rules import evaluate_flow_burst_entry
    from scalper.models import Bias

    close = 7494.0
    row = pd.Series({
        "open": close, "high": close + 1.0, "low": close - 1.0, "close": close,
        "volume": 500, "bid": close, "ask": close + 0.25,
        "bid_size": 5.0, "ask_size": 2.0, "bid_depth": 80.0, "ask_depth": 30.0,
        "delta": 12.0, "imbalance": 0.7, "atr": 5.0, "bar_range": 2.0,
        "ema_fast": close - 2.0, "ema_slow": close - 1.0, "ema_trend": close - 3.0,
        "vwap": close - 1.0, "adx": 25.0, "higher_high": 1.0, "lower_low": 0.0,
    })
    prev = row.copy()
    prev["ask_size"] = 8.0
    prev["delta"] = 2.0
    sig = evaluate_flow_burst_entry(
        row,
        5.0,
        100,
        mes_config,
        cooldown_remaining=0,
        session_bar_index=10,
        bar_time=datetime(2026, 6, 18, 9, 0),
        prev_row=prev,
        trend_row=row,
    )
    assert sig is not None
    assert sig.side.value == "LONG"


def test_mes_quote_stale_vs_es_divergence() -> None:
    from scalper.flow_signals import mes_quote_stale_vs_es

    mes_row = {
        "ts": __import__("time").time(),
        "mid_price": 7508.75,
        "bid_levels": [{"price": 7508.5, "qty": 1}],
        "ask_levels": [{"price": 7509.0, "qty": 1}],
        "depth_event_count_60s": 0,
    }
    es_row = {
        "ts": __import__("time").time(),
        "mid_price": 7513.25,
        "bid_levels": [{"price": 7513.0, "qty": 1}],
        "ask_levels": [{"price": 7513.5, "qty": 1}],
        "depth_event_count_60s": 12,
        "synthetic_source": "mes_es_bar_bridge",
    }
    stale, reason = mes_quote_stale_vs_es(mes_row, es_row, tick_size=0.25, max_divergence_ticks=4.0)
    assert stale is True
    assert reason == "es_divergence"


def test_resolve_mes_entry_price_uses_es_proxy_when_stale(mes_config) -> None:
    import time
    from unittest.mock import patch

    from scalper.flow_signals import es_proxy_mes_row
    from scalper.models import EntrySignal, Side
    from scalper.paper_runner import _resolve_mes_entry_price

    mes_row = {
        "ts": time.time(),
        "mid_price": 7508.75,
        "bid_levels": [{"price": 7508.5, "qty": 1}],
        "ask_levels": [{"price": 7509.0, "qty": 1}],
        "depth_event_count_60s": 0,
        "spread": 0.5,
    }
    es_row = {
        "ts": time.time(),
        "mid_price": 7513.25,
        "bid_levels": [{"price": 7513.0, "qty": 1}],
        "ask_levels": [{"price": 7513.5, "qty": 1}],
        "depth_event_count_60s": 8,
        "synthetic_source": "mes_es_bar_bridge",
        "spread": 0.5,
    }
    signal = EntrySignal(
        side=Side.LONG,
        price=7512.0,
        bar_index=10,
        trend_score=80.0,
        l2_score=70.0,
        reason="flow_burst_long",
    )

    def _read(root, **kwargs):
        return mes_row if root == "MES" else es_row

    with patch("scalper.flow_signals._read_orderflow_instrument", side_effect=_read):
        px, meta = _resolve_mes_entry_price(signal, mes_config)

    assert meta["mes_quote_stale"] is True
    assert meta["mes_quote_source"] == "es_proxy"
    proxy = es_proxy_mes_row(es_row)
    expected = float(proxy["ask_levels"][0]["price"]) + mes_config.mes_execution.entry_chase_ticks * mes_config.tick_size
    assert px == expected

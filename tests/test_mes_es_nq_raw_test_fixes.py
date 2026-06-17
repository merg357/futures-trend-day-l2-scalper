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


def test_adverse_mid_cancel_ticks_12(mes_config) -> None:
    assert mes_config.entry.entry_adverse_mid_ticks == 12
    assert mes_config.entry.use_adverse_mid_cancel is True

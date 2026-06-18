"""Routing tests — MES execution, ES signal, NQ veto; no ES/NQ orders."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from scalper.config import load_config
from scalper.entry_rules import evaluate_flow_burst_entry
from scalper.live_gateway import LiveGateway, OrderAction, OrderRequest, _normalize_protective_stop
from scalper.mes_es_nq_runner import mes_entry_blocked_reason
from scalper.models import Side
from scalper.nq_confirmation import nq_veto_reason

ROOT = Path(__file__).resolve().parents[1]
MES_CONFIG = ROOT / "configs" / "production" / "mes_es_nq_raw_test.yaml"


@pytest.fixture
def mes_config():
    return load_config(MES_CONFIG)


def test_config_mode_and_routing(mes_config) -> None:
    assert mes_config.is_mes_es_nq_mode()
    assert mes_config.execution_root() == "MES"
    assert mes_config.signal_root() == "ES"
    assert mes_config.confirmation_root() == "NQ"
    assert mes_config.filters.use_take_profit is False
    assert mes_config.exit.stop_loss_ticks == 10


def test_mnq_config_unchanged() -> None:
    mnq = load_config(ROOT / "configs" / "production" / "mnq_walkforward_optimized.yaml")
    assert not mnq.is_mes_es_nq_mode()
    assert mnq.symbol == "MNQ"


def test_nq_veto_blocks_es_long(mes_config) -> None:
    nq_row = pd.Series(
        {
            "delta": -20,
            "imbalance": -0.8,
            "mbo_bid_new_count": 1,
            "mbo_ask_new_count": 10,
        }
    )
    reason = nq_veto_reason(Side.LONG, mes_config, nq_row=nq_row)
    assert reason is not None
    assert "nq_veto" in reason


def test_nq_veto_allows_when_disabled(mes_config) -> None:
    mes_config.nq_confirmation.use_nq_confirmation = False
    nq_row = pd.Series({"delta": -20, "imbalance": -0.8})
    assert nq_veto_reason(Side.LONG, mes_config, nq_row=nq_row) is None


def test_flow_burst_blocked_by_nq_veto(mes_config) -> None:
    from scalper.models import Bias
    from scalper.trend_score import TrendScore

    es_row = pd.Series(
        {
            "close": 5500.0,
            "high": 5500.0,
            "low": 5500.0,
            "open": 5500.0,
            "delta": 12,
            "imbalance": 0.7,
            "bid_size": 100,
            "ask_size": 20,
        }
    )
    trend = TrendScore(score=80.0, bias=Bias.LONG, components={})
    with patch("scalper.entry_rules.nq_veto_comparison", return_value={"nq_veto_reason": "nq_veto_bearish score=70", "nq_veto_soft_blocks": True}):
        with patch("scalper.entry_rules.compute_trend_score", return_value=trend):
            with patch("scalper.entry_rules.compute_flow_signal") as mock_flow:
                from scalper.flow_signals import FlowSignal

                mock_flow.return_value = FlowSignal(
                    side=Side.LONG, score=75.0, triggers_hit=2, triggers={"delta": True, "imbalance": True}
                )
                with patch("scalper.entry_rules.flow_burst_passes", return_value=True):
                    with patch("scalper.entry_rules.compute_flow_for_side") as mock_side:
                        mock_side.return_value = FlowSignal(
                            side=Side.LONG,
                            score=75.0,
                            triggers_hit=2,
                            triggers={"delta": True, "imbalance": True},
                        )
                        signal = evaluate_flow_burst_entry(
                            es_row,
                            5.0,
                            10,
                            mes_config,
                            0,
                            100,
                            pd.Timestamp("2026-06-17 10:00:00"),
                        )
    assert signal is None


def test_gateway_submit_order_uses_mes_execution_symbol(mes_config) -> None:
    gw = LiveGateway(
        log_dir=ROOT / "data" / "test_mes_routing",
        execution_symbol="MES",
        order_prefix="L2MES",
    )
    with patch("scalper.live_gateway.nt8_orders_enabled", return_value=False):
        result = gw.submit_order(
            OrderRequest(
                action=OrderAction.ENTER,
                symbol="ES",
                side=Side.LONG,
                quantity=1,
                price=5500.0,
                reason="test_routing",
            )
        )
    assert result["status"] == "blocked_paper_mode"
    assert gw._execution_symbol == "MES"


def test_submit_order_never_uses_es_or_nq_for_execution(mes_config) -> None:
    gw = LiveGateway(
        execution_symbol=mes_config.execution_root(),
        order_prefix=mes_config.mes_execution.order_id_prefix,
    )
    assert gw._execution_symbol == "MES"
    assert gw._execution_symbol not in ("ES", "NQ")


def test_exit_no_take_profit(mes_config) -> None:
    from scalper.exit_rules import init_position

    pos = init_position(
        Side.LONG, 5500.0, 0, pd.Timestamp("2026-06-17 10:00:00"), 1, mes_config
    )
    assert pos.target_price > 6000.0


def test_long_stop_normalized_below_bid() -> None:
    """LONG protective stop must sit below live bid (NT8 rejects sell stop above market)."""
    with patch(
        "scalper.live_gateway._read_exec_market_quote",
        return_value={"bid": 7510.0, "ask": 7510.25, "mid": 7510.125},
    ):
        stop = _normalize_protective_stop(
            Side.LONG,
            7511.5,
            symbol="MES",
            tick=0.25,
            stop_ticks=10,
        )
    assert stop < 7510.0


def test_submit_stop_order_blocks_when_still_above_bid() -> None:
    gw = LiveGateway(
        log_dir=ROOT / "data" / "test_mes_routing",
        execution_symbol="MES",
        order_prefix="L2MES",
    )
    with patch("scalper.live_gateway.nt8_orders_enabled", return_value=True):
        with patch("scalper.live_gateway.require_live_trading"):
            with patch(
                "scalper.live_gateway._read_exec_market_quote",
                return_value={"bid": 7510.0, "ask": 7510.25, "mid": 7510.125},
            ):
                with patch(
                    "scalper.live_gateway._normalize_protective_stop",
                    return_value=7510.25,
                ):
                    result = gw.submit_stop_order(
                        side=Side.LONG,
                        quantity=1,
                        stop_price=7512.0,
                        reason="initial_hard_stop",
                    )
    assert result["status"] == "blocked_stop_above_bid"


def test_mes_entry_blocked_when_nt8_position_nonzero(mes_config) -> None:
    gw = LiveGateway(execution_symbol="MES", order_prefix="L2MES")
    gw.query_market_position = lambda: 1  # type: ignore[method-assign]
    with patch("scalper.mes_es_nq_runner._nt8_client_api_listening", return_value=True):
        assert mes_entry_blocked_reason(gw, mes_config) == "mes_position_nonzero=1"


def test_mes_entry_blocked_when_pending_ent_working(mes_config) -> None:
    gw = LiveGateway(execution_symbol="MES", order_prefix="L2MES")
    gw.query_market_position = lambda: 0  # type: ignore[method-assign]
    with patch("scalper.mes_es_nq_runner._nt8_client_api_listening", return_value=True):
        with patch(
            "scalper.mes_es_nq_runner._has_working_ent_order",
            return_value=(True, "L2MES_ENT_flow_burst_123_1"),
        ):
            reason = mes_entry_blocked_reason(gw, mes_config)
    assert reason == "pending_l2mes_ent_working=L2MES_ENT_flow_burst_123_1"


def test_mes_entry_blocked_when_nt8_client_api_down(mes_config) -> None:
    gw = LiveGateway(execution_symbol="MES", order_prefix="L2MES")
    with patch("scalper.mes_es_nq_runner._nt8_client_api_listening", return_value=False):
        assert mes_entry_blocked_reason(gw, mes_config) == "nt8_client_api_down"

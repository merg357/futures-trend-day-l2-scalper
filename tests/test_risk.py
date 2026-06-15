"""Tests for risk manager."""

from datetime import datetime

from scalper.config import load_config
from scalper.models import ExitReason, Side, Trade
from scalper.risk import RiskManager

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _trade(pnl: float) -> Trade:
    return Trade(
        side=Side.LONG,
        entry_time=datetime(2024, 6, 3, 10, 0),
        exit_time=datetime(2024, 6, 3, 10, 5),
        entry_price=100.0,
        exit_price=101.0,
        quantity=1,
        pnl=pnl,
        pnl_ticks=4,
        commission=1.24,
        exit_reason=ExitReason.TARGET,
        bars_held=5,
    )


def test_max_trades_halts() -> None:
    cfg = load_config(ROOT / "configs" / "mnq_default.yaml")
    cfg.risk.max_trades_per_session = 2
    rm = RiskManager(cfg)
    rm.record_trade(_trade(10))
    rm.record_trade(_trade(-5))
    assert rm.can_enter() is False


def test_consecutive_losses_halts() -> None:
    cfg = load_config(ROOT / "configs" / "mnq_default.yaml")
    cfg.risk.max_consecutive_losses = 2
    rm = RiskManager(cfg)
    rm.record_trade(_trade(-10))
    rm.record_trade(_trade(-10))
    assert rm.halted is True

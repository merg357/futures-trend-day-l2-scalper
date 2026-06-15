"""Tests for configuration loading."""

from pathlib import Path

from scalper.config import ScalperConfig, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_load_mnq_config() -> None:
    cfg = load_config(ROOT / "configs" / "mnq_default.yaml")
    assert cfg.symbol == "MNQ"
    assert cfg.tick_size == 0.25
    assert cfg.trend.min_trend_score == 58
    assert cfg.l2.min_l2_score == 52


def test_load_mes_config() -> None:
    cfg = load_config(ROOT / "configs" / "mes_default.yaml")
    assert cfg.symbol == "MES"
    assert cfg.tick_value == 1.25
    assert cfg.exit.stop_loss_ticks == 8


def test_config_validation() -> None:
    cfg = ScalperConfig(symbol="TEST")
    assert cfg.session.rth_open == "09:30"

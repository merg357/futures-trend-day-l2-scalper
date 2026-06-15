"""Integration tests for backtester."""

from pathlib import Path

import pytest

from scalper.backtest import run_backtest_from_paths
from scalper.sample_data import generate_all_samples

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def sample_data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("data")
    generate_all_samples(out, n_bars=80)
    return out


def test_backtest_trend_up(sample_data: Path) -> None:
    result = run_backtest_from_paths(
        ROOT / "configs" / "mnq_default.yaml",
        sample_data / "mnq_trend_up.csv",
    )
    assert result.bars_processed > 0
    assert result.metrics.total_trades >= 0
    assert len(result.equity_curve) > 0


def test_backtest_chop_fewer_trades(sample_data: Path) -> None:
    trend = run_backtest_from_paths(
        ROOT / "configs" / "mnq_default.yaml",
        sample_data / "mnq_trend_up.csv",
    )
    chop = run_backtest_from_paths(
        ROOT / "configs" / "mnq_default.yaml",
        sample_data / "mnq_chop.csv",
    )
    assert chop.metrics.total_trades <= trend.metrics.total_trades + 5


def test_l2_warning_without_columns(sample_data: Path, tmp_path: Path) -> None:
    import pandas as pd

    df = pd.read_csv(sample_data / "mnq_trend_up.csv")
    df = df.drop(columns=["bid_size", "ask_size", "bid_depth", "ask_depth"], errors="ignore")
    path = tmp_path / "no_l2.csv"
    df.to_csv(path, index=False)
    result = run_backtest_from_paths(ROOT / "configs" / "mnq_default.yaml", path)
    assert result.l2_approximated
    assert any("approximation" in w.lower() for w in result.warnings)

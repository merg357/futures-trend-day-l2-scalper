"""Tests for multi-timeframe backtest."""

from pathlib import Path

from scalper.mtf_backtest import prepare_mtf_dataframe, run_mtf_backtest_from_paths
from scalper.sample_data import generate_all_samples
from scalper.config import load_config

ROOT = Path(__file__).resolve().parents[1]


def test_prepare_mtf_dataframe(tmp_path: Path) -> None:
    generate_all_samples(tmp_path, n_bars=120)
    df = __import__("pandas").read_csv(tmp_path / "mnq_trend_up.csv", parse_dates=["timestamp"])
    cfg = load_config(ROOT / "configs" / "mnq_mtf.yaml")
    mtf = prepare_mtf_dataframe(df, cfg, trend_minutes=5)
    assert "mtf_ema_fast" in mtf.columns
    assert "mtf_adx" in mtf.columns
    assert len(mtf) == len(df)


def test_mtf_backtest_runs(tmp_path: Path) -> None:
    generate_all_samples(tmp_path, n_bars=120)
    result = run_mtf_backtest_from_paths(
        ROOT / "configs" / "mnq_mtf.yaml",
        tmp_path / "mnq_trend_up.csv",
    )
    assert result.bars_processed > 0
    assert any("MTF mode" in w for w in result.warnings)

"""Tests for report generation."""

from pathlib import Path

from scalper.backtest import run_backtest_from_paths
from scalper.reports import generate_report
from scalper.sample_data import generate_trend_up

ROOT = Path(__file__).resolve().parents[1]


def test_generate_all_report_formats(tmp_path: Path) -> None:
    data_path = tmp_path / "data.csv"
    generate_trend_up(60).to_csv(data_path, index=False)
    result = run_backtest_from_paths(ROOT / "configs" / "mnq_default.yaml", data_path)
    out = tmp_path / "reports"
    paths = generate_report(result, out)
    assert paths["json"].exists()
    assert paths["csv"].exists()
    assert paths["html"].exists()
    charts = paths["charts"]
    assert any(p.name == "equity_curve.png" for p in charts)

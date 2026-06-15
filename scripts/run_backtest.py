#!/usr/bin/env python3
"""Run a single backtest and write reports."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rich.console import Console
from rich.table import Table

from scalper.backtest import run_backtest_from_paths
from scalper.reports import generate_report

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run trend-day L2 scalper backtest")
    parser.add_argument("--config", required=True, help="Path to YAML config")
    parser.add_argument("--data", required=True, help="Path to CSV bar data")
    parser.add_argument("--out", required=True, help="Output report directory")
    args = parser.parse_args()

    config_path = ROOT / args.config if not Path(args.config).is_absolute() else Path(args.config)
    data_path = ROOT / args.data if not Path(args.data).is_absolute() else Path(args.data)
    out_dir = ROOT / args.out if not Path(args.out).is_absolute() else Path(args.out)

    result = run_backtest_from_paths(config_path, data_path)
    generate_report(result, out_dir)

    table = Table(title=f"Backtest: {result.symbol}")
    table.add_column("Metric")
    table.add_column("Value")
    m = result.metrics
    for label, val in [
        ("Trades", m.total_trades),
        ("Win Rate", f"{m.win_rate:.1%}"),
        ("Net PnL", f"${m.net_pnl:.2f}"),
        ("Profit Factor", f"{m.profit_factor:.2f}"),
        ("Max Drawdown", f"${m.max_drawdown:.2f}"),
    ]:
        table.add_row(label, str(val))
    console.print(table)

    if result.warnings:
        for w in result.warnings:
            console.print(f"[yellow]Warning: {w}[/yellow]")

    summary_path = out_dir / "summary.json"
    summary_path.write_text(
        json.dumps({"metrics": m.model_dump(), "warnings": result.warnings, "l2_approximated": result.l2_approximated}, indent=2),
        encoding="utf-8",
    )
    console.print(f"[green]Reports written to {out_dir}[/green]")


if __name__ == "__main__":
    main()

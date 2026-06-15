#!/usr/bin/env python3
"""Run backtests on all real-data CSV files in data/raw."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from scalper.backtest import run_backtest_from_paths
from scalper.reports import generate_report

RAW = ROOT / "data" / "raw"
DAY_PATTERN = re.compile(r"^(MNQ|MES)_(\d{8})_1m\.csv$", re.IGNORECASE)


def config_for(file_path: Path) -> Path:
    name = file_path.name.upper()
    if name.startswith("MES"):
        return ROOT / "configs" / "mes_default.yaml"
    return ROOT / "configs" / "mnq_default.yaml"


def day_csv_files() -> list[Path]:
    return sorted(p for p in RAW.glob("*_1m.csv") if DAY_PATTERN.match(p.name))


def aggregate_stats(rows: list[dict]) -> dict:
    if not rows:
        return {}
    total_trades = sum(r["trades"] for r in rows)
    wins = sum(r["trades"] * r["win_rate"] for r in rows)
    net_pnl = sum(r["net_pnl"] for r in rows)
    gross_profit = sum(r.get("gross_profit", 0) for r in rows)
    gross_loss = sum(r.get("gross_loss", 0) for r in rows)
    pf = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    max_dd = max(r["max_drawdown"] for r in rows) if rows else 0.0
    return {
        "days": len(rows),
        "trades": total_trades,
        "win_rate": round(wins / total_trades, 4) if total_trades else 0.0,
        "net_pnl": round(net_pnl, 2),
        "profit_factor": round(min(pf, 999.0), 2),
        "max_drawdown": round(max_dd, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch backtest real L2 CSV data")
    parser.add_argument("--reports-dir", default="data/reports/real_full", help="Output report directory")
    args = parser.parse_args()

    reports = ROOT / args.reports_dir
    reports.mkdir(parents=True, exist_ok=True)

    csv_files = day_csv_files()
    if not csv_files:
        raise SystemExit("No per-day *_YYYYMMDD_1m.csv files in data/raw")

    summaries = []
    by_instrument: dict[str, list[dict]] = {"MNQ": [], "MES": []}

    for csv_path in csv_files:
        m = DAY_PATTERN.match(csv_path.name)
        assert m is not None
        inst, date = m.group(1).upper(), m.group(2)
        out_dir = reports / f"{inst}_{date}"
        out_dir.mkdir(parents=True, exist_ok=True)
        cfg = config_for(csv_path)
        result = run_backtest_from_paths(cfg, csv_path)
        generate_report(result, out_dir)
        metrics = result.metrics
        row = {
            "file": csv_path.name,
            "instrument": inst,
            "date": date,
            "symbol": result.symbol,
            "trades": metrics.total_trades,
            "win_rate": round(metrics.win_rate, 4),
            "net_pnl": round(metrics.net_pnl, 2),
            "profit_factor": round(metrics.profit_factor, 2),
            "max_drawdown": round(metrics.max_drawdown, 2),
            "sharpe": round(metrics.sharpe_ratio, 4),
            "gross_profit": round(metrics.avg_win * metrics.winning_trades, 2),
            "gross_loss": round(abs(metrics.avg_loss) * metrics.losing_trades, 2),
            "long_trades": sum(1 for t in result.trades if str(t["side"]).lower() == "long"),
            "short_trades": sum(1 for t in result.trades if str(t["side"]).lower() == "short"),
            "l2_approximated": result.l2_approximated,
            "bars": result.bars_processed,
            "report_html": str(out_dir / "report.html"),
        }
        summaries.append(row)
        by_instrument[inst].append(row)
        print(f"{csv_path.name}: trades={metrics.total_trades} pnl={metrics.net_pnl:.2f} wr={metrics.win_rate:.1%}")

    combined_summaries = []
    for prefix in ("MNQ", "MES"):
        parts = [pd.read_csv(p, parse_dates=["timestamp"]) for p in csv_files if p.name.upper().startswith(prefix)]
        if not parts:
            continue
        combined = pd.concat(parts, ignore_index=True).sort_values("timestamp")
        combined_path = RAW / f"{prefix}_combined_1m.csv"
        combined.to_csv(combined_path, index=False)
        out_dir = reports / f"{prefix}_combined"
        result = run_backtest_from_paths(config_for(combined_path), combined_path)
        generate_report(result, out_dir)
        m = result.metrics
        combined_row = {
            "file": combined_path.name,
            "instrument": prefix,
            "date": "combined",
            "symbol": result.symbol,
            "trades": m.total_trades,
            "win_rate": round(m.win_rate, 4),
            "net_pnl": round(m.net_pnl, 2),
            "profit_factor": round(m.profit_factor, 2),
            "max_drawdown": round(m.max_drawdown, 2),
            "sharpe": round(m.sharpe_ratio, 4),
            "gross_profit": round(m.avg_win * m.winning_trades, 2),
            "gross_loss": round(abs(m.avg_loss) * m.losing_trades, 2),
            "long_trades": sum(1 for t in result.trades if str(t["side"]).lower() == "long"),
            "short_trades": sum(1 for t in result.trades if str(t["side"]).lower() == "short"),
            "l2_approximated": result.l2_approximated,
            "bars": result.bars_processed,
            "report_html": str(out_dir / "report.html"),
        }
        combined_summaries.append(combined_row)
        summaries.append(combined_row)
        print(f"COMBINED {prefix}: trades={m.total_trades} pnl={m.net_pnl:.2f} wr={m.win_rate:.1%}")

    aggregate = {
        "MNQ": aggregate_stats(by_instrument["MNQ"]),
        "MES": aggregate_stats(by_instrument["MES"]),
    }
    for inst in ("MNQ", "MES"):
        days = by_instrument[inst]
        if days:
            best = max(days, key=lambda r: r["net_pnl"])
            worst = min(days, key=lambda r: r["net_pnl"])
            aggregate[inst]["best_day"] = {"date": best["date"], "net_pnl": best["net_pnl"]}
            aggregate[inst]["worst_day"] = {"date": worst["date"], "net_pnl": worst["net_pnl"]}

    payload = {
        "per_day": [r for r in summaries if r.get("date") != "combined"],
        "combined": combined_summaries,
        "aggregate": aggregate,
    }
    summary_path = reports / "real_backtest_summary.json"
    summary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Summary -> {summary_path}")


if __name__ == "__main__":
    main()

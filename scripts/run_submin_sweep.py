#!/usr/bin/env python3
"""MNQ sub-minute (10s/15s/30s) ETL, backtest, optimization, and report generation."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scalper.backtest import run_backtest_from_paths
from scalper.config import ScalperConfig, config_to_dict, load_config
from scalper.l2_etl import INTERVAL_SECONDS, convert_archive_to_bars, default_output_dir
from scalper.optimize import run_optimization
from scalper.reports import generate_report

RAW = ROOT / "data" / "raw"
SUBMIN = RAW / "submin"
OUT_ROOT = ROOT / "data" / "reports" / "submin_mnq"
OPT_DIR = OUT_ROOT / "optimization"
BACKTEST_DIR = OUT_ROOT / "backtests"

SUBMIN_INTERVALS = ("10s", "15s", "30s")

TIMEFRAME_CONFIGS: dict[str, str] = {
    "10s": "configs/mnq_10s.yaml",
    "15s": "configs/mnq_15s.yaml",
    "30s": "configs/mnq_30s.yaml",
    "1m": "configs/mnq_default.yaml",
}

PARAM_MAP: dict[str, tuple[str, str]] = {
    "min_trend_score": ("trend", "min_trend_score"),
    "min_l2_score": ("l2", "min_l2_score"),
    "adx_trend_min": ("trend", "adx_trend_min"),
    "atr_expansion_mult": ("trend", "atr_expansion_mult"),
    "imbalance_threshold": ("l2", "imbalance_threshold"),
    "min_book_depth": ("l2", "min_book_depth"),
    "max_spread_ticks": ("entry", "max_spread_ticks"),
    "pullback_to_ema_ticks": ("entry", "pullback_to_ema_ticks"),
    "stop_loss_ticks": ("exit", "stop_loss_ticks"),
    "take_profit_ticks": ("exit", "take_profit_ticks"),
    "breakeven_trigger_ticks": ("exit", "breakeven_trigger_ticks"),
    "trailing_trigger_ticks": ("exit", "trailing_trigger_ticks"),
    "trailing_offset_ticks": ("exit", "trailing_offset_ticks"),
    "max_hold_bars": ("exit", "max_hold_bars"),
}

DAY_PATTERN = re.compile(r"^MNQ_(\d{8})_(10s|15s|30s|1m)\.csv$", re.IGNORECASE)


def load_mnq_dates_from_manifest() -> list[str]:
    manifest = RAW / "etl_manifest.json"
    dates: set[str] = set()

    if manifest.is_file():
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        for f in payload.get("files", []):
            if f.get("instrument") != "MNQ":
                continue
            interval = f.get("interval", "1m")
            if interval == "1m":
                dates.add(f["date"])

    # Fallback: discover from existing per-day 1m CSV files
    for path in RAW.glob("MNQ_*_1m.csv"):
        m = re.match(r"^MNQ_(\d{8})_1m\.csv$", path.name, re.IGNORECASE)
        if m:
            dates.add(m.group(1))

    return sorted(dates)


def run_etl(dates: list[str], skip_existing: bool = True) -> list[dict[str, Any]]:
    SUBMIN.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    errors: list[str] = []

    for interval in SUBMIN_INTERVALS:
        for date in dates:
            out_path = SUBMIN / f"MNQ_{date}_{interval}.csv"
            if skip_existing and out_path.is_file():
                print(f"SKIP ETL MNQ {date} {interval}")
                continue
            try:
                result = convert_archive_to_bars("MNQ", date, output_dir=SUBMIN, interval=interval)
                results.append(result.to_dict())
                print(f"OK ETL MNQ {date} {interval} -> {result.row_count} bars")
            except Exception as exc:  # noqa: BLE001
                msg = f"MNQ {date} {interval}: {exc}"
                errors.append(msg)
                print(f"ERR {msg}")

    if errors:
        (OUT_ROOT / "etl_errors.log").write_text("\n".join(errors) + "\n", encoding="utf-8")

    manifest_sub = OUT_ROOT / "submin_etl_manifest.json"
    manifest_sub.write_text(json.dumps({"files": results, "errors": errors}, indent=2), encoding="utf-8")
    return results


def build_combined_csv(interval: str) -> Path:
    parts = sorted(SUBMIN.glob(f"MNQ_*_{interval}.csv"))
    if not parts:
        raise FileNotFoundError(f"No per-day MNQ *_{interval}.csv in {SUBMIN}")
    frames = [pd.read_csv(p, parse_dates=["timestamp"]) for p in parts]
    combined = pd.concat(frames, ignore_index=True).sort_values("timestamp")
    out_path = SUBMIN / f"MNQ_combined_{interval}.csv"
    combined.to_csv(out_path, index=False)
    print(f"Combined {interval}: {len(parts)} days, {len(combined)} bars -> {out_path.name}")
    return out_path


def apply_params_to_config(base_path: Path, best_params: dict[str, Any], out_path: Path) -> None:
    cfg = load_config(base_path)
    for key, value in best_params.items():
        if key in PARAM_MAP:
            section, attr = PARAM_MAP[key]
            setattr(getattr(cfg, section), attr, value)
    data = config_to_dict(cfg)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh, default_flow_style=False, sort_keys=False, allow_unicode=True)


def bar_interval_seconds(config_path: Path) -> int:
    cfg = load_config(config_path)
    return cfg.backtest.bar_interval_seconds


def avg_hold_seconds(result_trades: list[dict[str, Any]], interval_sec: int) -> float:
    if not result_trades:
        return 0.0
    holds = [t.get("bars_held", 0) * interval_sec for t in result_trades]
    return float(sum(holds) / len(holds))


def run_backtest_row(
    label: str,
    config_path: Path,
    data_path: Path,
    out_dir: Path,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    row: dict[str, Any] = {
        "label": label,
        "config": str(config_path.relative_to(ROOT)),
        "data": data_path.name,
        "interval": _interval_from_name(data_path.name),
    }
    try:
        result = run_backtest_from_paths(config_path, data_path)
        generate_report(result, out_dir)
        m = result.metrics
        interval_sec = bar_interval_seconds(config_path)
        row.update(
            {
                "trades": m.total_trades,
                "win_rate": m.win_rate,
                "net_pnl": m.net_pnl,
                "profit_factor": m.profit_factor,
                "max_drawdown": m.max_drawdown,
                "avg_bars_held": m.avg_bars_held,
                "avg_hold_seconds": avg_hold_seconds(result.trades, interval_sec),
                "l2_approximated": result.l2_approximated,
                "error": None,
            }
        )
        exit_cfg = load_config(config_path).exit
        row["exit_params"] = {
            "stop_loss_ticks": exit_cfg.stop_loss_ticks,
            "take_profit_ticks": exit_cfg.take_profit_ticks,
            "breakeven_trigger_ticks": exit_cfg.breakeven_trigger_ticks,
            "trailing_trigger_ticks": exit_cfg.trailing_trigger_ticks,
            "trailing_offset_ticks": exit_cfg.trailing_offset_ticks,
            "max_hold_bars": exit_cfg.max_hold_bars,
        }
        entry_cfg = load_config(config_path)
        row["entry_params"] = {
            "min_trend_score": entry_cfg.trend.min_trend_score,
            "min_l2_score": entry_cfg.l2.min_l2_score,
            "pullback_to_ema_ticks": entry_cfg.entry.pullback_to_ema_ticks,
            "max_spread_ticks": entry_cfg.entry.max_spread_ticks,
        }
        (out_dir / "summary.json").write_text(
            json.dumps({"metrics": m.model_dump(), "row": row}, indent=2),
            encoding="utf-8",
        )
        print(f"OK {label}: pnl={m.net_pnl:.2f} trades={m.total_trades} wr={m.win_rate:.1%}")
    except Exception as exc:
        row.update(
            {
                "trades": 0,
                "win_rate": 0.0,
                "net_pnl": 0.0,
                "profit_factor": 0.0,
                "max_drawdown": 0.0,
                "avg_bars_held": 0.0,
                "avg_hold_seconds": 0.0,
                "l2_approximated": False,
                "error": str(exc),
            }
        )
        print(f"FAIL {label}: {exc}")
    return row


def _interval_from_name(name: str) -> str:
    m = DAY_PATTERN.match(name)
    if m:
        return m.group(2).lower()
    if "combined_10s" in name.lower():
        return "10s"
    if "combined_15s" in name.lower():
        return "15s"
    if "combined_30s" in name.lower():
        return "30s"
    if "combined_1m" in name.lower():
        return "1m"
    return "unknown"


def run_optimizations(
    intervals: tuple[str, ...],
    trials: int,
    skip_existing: bool,
) -> dict[str, dict[str, Any]]:
    OPT_DIR.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict[str, Any]] = {}

    for interval in intervals:
        result_path = OPT_DIR / interval / "optimization_result.json"
        base_config = ROOT / TIMEFRAME_CONFIGS[interval]
        data_path = SUBMIN / f"MNQ_combined_{interval}.csv"

        if skip_existing and result_path.exists():
            results[interval] = json.loads(result_path.read_text(encoding="utf-8"))
            print(f"SKIP opt {interval}")
            continue

        print(f"RUN opt {interval}: {trials} trials, scalping=True")
        opt_result = run_optimization(
            base_config,
            data_path,
            n_trials=trials,
            focus="all",
            scalping=True,
        )
        opt_result["interval"] = interval
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(opt_result, indent=2, default=str), encoding="utf-8")

        optimized_path = ROOT / "configs" / f"mnq_{interval}_optimized.yaml"
        apply_params_to_config(base_config, opt_result["best_params"], optimized_path)
        opt_result["optimized_config"] = str(optimized_path.relative_to(ROOT))
        results[interval] = opt_result
        print(f"  best net_pnl={opt_result['best_value']:.2f} trades={opt_result['final_trades']}")

    return results


def fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _exit_params_str(params: dict[str, Any] | None) -> str:
    if not params:
        return "—"
    return (
        f"SL={params.get('stop_loss_ticks')} TP={params.get('take_profit_ticks')} "
        f"BE={params.get('breakeven_trigger_ticks')} "
        f"Trail={params.get('trailing_trigger_ticks')}/{params.get('trailing_offset_ticks')} "
        f"Hold={params.get('max_hold_bars')}b"
    )


def generate_report_md(
    backtest_rows: list[dict[str, Any]],
    opt_results: dict[str, dict[str, Any]],
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# MNQ Sub-Minute Backtest Report",
        "",
        f"Generated: {now}",
        "",
        "Research only — not for live trading.",
        "",
        "## Methodology",
        "",
        "- **Data:** Rithmic L2 archives resampled to 10s / 15s / 30s RTH bars (09:30–16:00 ET).",
        "- **Indicators:** EMA/ADX/ATR periods are **bar-count** (not wall-clock scaled). "
        "On sub-minute bars this means faster effective lookback (e.g. EMA-21 on 10s ≈ 3.5 min).",
        "- **L2 depth:** Archives provide L1 bid/ask size from trade snapshots; L2–L5 depth columns are approximated.",
        "- **Optimization:** Optuna `--focus all` with scalping ranges (SL 8–40 ticks, entry+exit filters), "
        "metric `net_pnl`, 70 trials per timeframe.",
        "",
        "## Comparison: 10s vs 15s vs 30s vs 1m baseline",
        "",
        "| Timeframe | Config | Trades | Win % | Net PnL | PF | Max DD | Avg hold (sec) | Exit params |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]

    # Pick best row per timeframe (optimized preferred, else baseline)
    by_tf: dict[str, list[dict[str, Any]]] = {}
    for row in backtest_rows:
        if row.get("error"):
            continue
        tf = row.get("interval", "unknown")
        by_tf.setdefault(tf, []).append(row)

    summary_rows: list[dict[str, Any]] = []
    for tf in ("10s", "15s", "30s", "1m"):
        candidates = by_tf.get(tf, [])
        if not candidates:
            continue
        optimized = [r for r in candidates if "optimized" in r.get("label", "") and not r["label"].startswith("1m")]
        baseline_tf = [r for r in candidates if r.get("label", "").endswith("_baseline")]
        baseline_1m = [r for r in candidates if r.get("label") == "1m_mnq_default"]
        if tf == "1m":
            pick = baseline_1m[0] if baseline_1m else candidates[0]
        else:
            pick = optimized[0] if optimized else (baseline_tf[0] if baseline_tf else candidates[0])
        summary_rows.append(pick)

        lines.append(
            f"| {tf} | {pick['label']} | {pick['trades']} | {fmt_pct(pick['win_rate'])} | "
            f"{fmt_money(pick['net_pnl'])} | {pick['profit_factor']:.2f} | "
            f"{fmt_money(pick['max_drawdown'])} | {pick.get('avg_hold_seconds', 0):.0f} | "
            f"{_exit_params_str(pick.get('exit_params'))} |"
        )

    if summary_rows:
        all_picks = [r for r in backtest_rows if not r.get("error")]
        winner = max(all_picks, key=lambda r: r.get("net_pnl", 0))
        best_submin = max(
            [r for r in summary_rows if r.get("interval") in SUBMIN_INTERVALS],
            key=lambda r: r.get("net_pnl", 0),
            default=None,
        )
        lines.extend(
            [
                "",
                f"**Best overall (combined real MNQ):** `{winner['interval']}` / {winner['label']} "
                f"(net PnL {fmt_money(winner['net_pnl'])})",
            ]
        )
        if best_submin:
            lines.append(
                f"**Best sub-minute optimized:** `{best_submin['interval']}` / {best_submin['label']} "
                f"(net PnL {fmt_money(best_submin['net_pnl'])})"
            )

    lines.extend(
        [
            "",
            "## All backtest runs",
            "",
            "| Label | Data | Trades | Win % | Net PnL | PF | Max DD | Avg hold (sec) |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in sorted(backtest_rows, key=lambda r: (r.get("interval", ""), r.get("label", ""))):
        if row.get("error"):
            lines.append(f"| {row['label']} | {row['data']} | ERR | — | — | — | — | — |")
            continue
        lines.append(
            f"| {row['label']} | {row['data']} | {row['trades']} | {fmt_pct(row['win_rate'])} | "
            f"{fmt_money(row['net_pnl'])} | {row['profit_factor']:.2f} | "
            f"{fmt_money(row['max_drawdown'])} | {row.get('avg_hold_seconds', 0):.0f} |"
        )

    lines.extend(["", "## Optimization best params", ""])
    for interval in SUBMIN_INTERVALS:
        opt = opt_results.get(interval)
        if not opt:
            continue
        lines.append(f"### {interval}")
        lines.append("")
        lines.append(f"- Best net_pnl: **{opt.get('best_value', 0):.2f}** ({opt.get('final_trades', 0)} trades)")
        lines.append(f"- Config: `{opt.get('optimized_config', '')}`")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(opt.get("best_params", {}), indent=2))
        lines.append("```")
        lines.append("")

    lines.extend(
        [
            "## Caveats",
            "",
            "1. **L1-only depth:** Most archives use trade-snapshot bid/ask size; multi-level book is approximated.",
            "2. **Overfitting:** 70 Optuna trials on a limited date range can overfit; validate on held-out days.",
            "3. **Bar-count indicators:** EMA-9/21/50 on 10s bars ≠ same signals as on 1m; compare wall-clock equivalents separately if needed.",
            "4. **Slippage/commission:** 1-tick slippage + $0.62/side assumed; sub-minute scalping may face higher real costs.",
            "5. **Combined vs per-day:** Single combined backtest differs from summing isolated daily runs.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="MNQ sub-minute ETL + backtest + optimization sweep")
    parser.add_argument("--skip-etl", action="store_true", help="Skip ETL (use existing submin CSVs)")
    parser.add_argument("--skip-opt", action="store_true", help="Skip Optuna optimization")
    parser.add_argument("--skip-backtest", action="store_true", help="Skip backtests")
    parser.add_argument("--trials", type=int, default=70, help="Optuna trials per timeframe")
    parser.add_argument("--force", action="store_true", help="Re-run even if outputs exist")
    args = parser.parse_args()

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    skip_existing = not args.force

    dates = load_mnq_dates_from_manifest()
    if not dates:
        raise SystemExit("No MNQ dates in data/raw/etl_manifest.json — run 1m ETL first")
    print(f"MNQ dates from manifest: {len(dates)}")

    if not args.skip_etl:
        run_etl(dates, skip_existing=skip_existing)

    for interval in SUBMIN_INTERVALS:
        build_combined_csv(interval)

    opt_results: dict[str, dict[str, Any]] = {}
    if not args.skip_opt:
        opt_results = run_optimizations(SUBMIN_INTERVALS, args.trials, skip_existing=skip_existing)

    backtest_rows: list[dict[str, Any]] = []
    if not args.skip_backtest:
        combined_1m = RAW / "MNQ_combined_1m.csv"
        if not combined_1m.is_file():
            parts = sorted(RAW.glob("MNQ_*_1m.csv"))
            if parts:
                frames = [pd.read_csv(p, parse_dates=["timestamp"]) for p in parts]
                pd.concat(frames, ignore_index=True).sort_values("timestamp").to_csv(combined_1m, index=False)

        runs: list[tuple[str, str, Path]] = []
        for interval in SUBMIN_INTERVALS:
            data_path = SUBMIN / f"MNQ_combined_{interval}.csv"
            base_cfg = ROOT / TIMEFRAME_CONFIGS[interval]
            opt_cfg = ROOT / f"configs/mnq_{interval}_optimized.yaml"
            runs.append((f"{interval}_baseline", str(base_cfg), data_path))
            if opt_cfg.is_file():
                runs.append((f"{interval}_optimized", str(opt_cfg), data_path))

        if combined_1m.is_file():
            runs.append(("1m_mnq_default", str(ROOT / "configs/mnq_default.yaml"), combined_1m))
            opt_1m = ROOT / "configs/mnq_entry_optimized.yaml"
            if opt_1m.is_file():
                runs.append(("1m_entry_optimized", str(opt_1m), combined_1m))

        for label, cfg_rel, data_path in runs:
            cfg_path = Path(cfg_rel) if Path(cfg_rel).is_absolute() else ROOT / cfg_rel
            out_dir = BACKTEST_DIR / label
            backtest_rows.append(run_backtest_row(label, cfg_path, data_path, out_dir))

        (OUT_ROOT / "backtest_results.json").write_text(
            json.dumps(backtest_rows, indent=2, default=str),
            encoding="utf-8",
        )

    report_md = generate_report_md(backtest_rows, opt_results)
    report_path = OUT_ROOT / "SUBMIN_MNQ_REPORT.md"
    report_path.write_text(report_md, encoding="utf-8")
    print(f"\nReport -> {report_path}")


if __name__ == "__main__":
    main()

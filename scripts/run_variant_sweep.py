#!/usr/bin/env python3
"""Comprehensive config-variant optimization and backtest sweep."""

from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scalper.backtest import run_backtest_from_paths
from scalper.config import ScalperConfig, config_to_dict, load_config
from scalper.optimize import run_optimization
from scalper.reports import generate_report

OUT_ROOT = ROOT / "data" / "reports" / "variant_sweep"
OPT_DIR = OUT_ROOT / "optimization"
BACKTEST_DIR = OUT_ROOT / "backtests"

DAY_PATTERN = re.compile(r"^(MNQ|MES|ES|NQ)_(\d{8})_1m\.csv$", re.IGNORECASE)
COMBINED_PATTERN = re.compile(r"^(MNQ|MES|ES|NQ)_combined_1m\.csv$", re.IGNORECASE)

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

CONFIG_VARIANTS: dict[str, dict[str, Any]] = {
    "mnq_default": {"path": "configs/mnq_default.yaml", "symbol": "MNQ"},
    "mnq_entry_optimized": {"path": "configs/mnq_entry_optimized.yaml", "symbol": "MNQ"},
    "mnq_exit_optimized": {"path": "configs/mnq_exit_optimized.yaml", "symbol": "MNQ"},
    "mnq_full_optimized": {"path": "configs/mnq_full_optimized.yaml", "symbol": "MNQ"},
    "mes_default": {"path": "configs/mes_default.yaml", "symbol": "MES"},
    "mes_entry_optimized": {"path": "configs/mes_entry_optimized.yaml", "symbol": "MES"},
    "mes_exit_only": {"path": "configs/mes_exit_only.yaml", "symbol": "MES"},
    "mes_full_optimized": {"path": "configs/mes_full_optimized.yaml", "symbol": "MES"},
}

OPTIMIZATION_RUNS: list[dict[str, Any]] = [
    {
        "id": "mnq_exit_default",
        "instrument": "MNQ",
        "focus": "exit",
        "base_config": "configs/mnq_optimize_net_pnl.yaml",
        "data": "data/raw/MNQ_combined_1m.csv",
        "trials": 80,
        "output_config": "configs/mnq_exit_optimized.yaml",
        "output_result": "mnq_exit_default",
    },
    {
        "id": "mnq_exit_on_entry",
        "instrument": "MNQ",
        "focus": "exit",
        "base_config": "configs/mnq_entry_optimized.yaml",
        "data": "data/raw/MNQ_combined_1m.csv",
        "trials": 80,
        "output_config": None,
        "output_result": "mnq_exit_on_entry",
    },
    {
        "id": "mnq_entry",
        "instrument": "MNQ",
        "focus": "filters",
        "base_config": "configs/mnq_optimize_net_pnl.yaml",
        "data": "data/raw/MNQ_combined_1m.csv",
        "trials": 80,
        "output_config": "configs/mnq_entry_optimized.yaml",
        "output_result": "mnq_entry",
    },
    {
        "id": "mnq_full",
        "instrument": "MNQ",
        "focus": "exit",
        "base_config": "configs/mnq_entry_optimized.yaml",
        "data": "data/raw/MNQ_combined_1m.csv",
        "trials": 80,
        "output_config": "configs/mnq_full_optimized.yaml",
        "output_result": "mnq_full",
    },
    {
        "id": "mes_entry",
        "instrument": "MES",
        "focus": "filters",
        "base_config": "configs/mes_optimize_net_pnl.yaml",
        "data": "data/raw/MES_combined_1m.csv",
        "trials": 80,
        "output_config": "configs/mes_entry_optimized.yaml",
        "output_result": "mes_entry",
    },
    {
        "id": "mes_exit_default",
        "instrument": "MES",
        "focus": "exit",
        "base_config": "configs/mes_optimize_net_pnl.yaml",
        "data": "data/raw/MES_combined_1m.csv",
        "trials": 80,
        "output_config": "configs/mes_exit_only.yaml",
        "output_result": "mes_exit_default",
    },
    {
        "id": "mes_exit_on_entry",
        "instrument": "MES",
        "focus": "exit",
        "base_config": "configs/mes_entry_optimized.yaml",
        "data": "data/raw/MES_combined_1m.csv",
        "trials": 80,
        "output_config": None,
        "output_result": "mes_exit_on_entry",
    },
    {
        "id": "mes_full",
        "instrument": "MES",
        "focus": "exit",
        "base_config": "configs/mes_entry_optimized.yaml",
        "data": "data/raw/MES_combined_1m.csv",
        "trials": 80,
        "output_config": "configs/mes_full_optimized.yaml",
        "output_result": "mes_full",
    },
]


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


def ensure_optimize_net_pnl_configs() -> None:
    for sym, default_name, out_name in [
        ("MNQ", "mnq_default.yaml", "mnq_optimize_net_pnl.yaml"),
        ("MES", "mes_default.yaml", "mes_optimize_net_pnl.yaml"),
    ]:
        src = ROOT / "configs" / default_name
        dst = ROOT / "configs" / out_name
        if dst.exists():
            continue
        cfg = load_config(src)
        data = config_to_dict(cfg)
        data["optimize"] = {"n_trials_default": 80, "metric": "net_pnl", "min_trades": 3}
        with dst.open("w", encoding="utf-8") as fh:
            yaml.dump(data, fh, default_flow_style=False, sort_keys=False)


def discover_data() -> dict[str, list[Path]]:
    raw = ROOT / "data" / "raw"
    sample = ROOT / "data" / "sample"
    found: dict[str, list[Path]] = {"MNQ": [], "MES": [], "ES": [], "other": []}

    for d in (raw, sample):
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.csv")):
            name = p.name.upper()
            if name.startswith("MNQ"):
                found["MNQ"].append(p)
            elif name.startswith("MES"):
                found["MES"].append(p)
            elif name.startswith("ES"):
                found["ES"].append(p)
            else:
                found["other"].append(p)
    return found


def config_for_data(data_path: Path) -> str | None:
    name = data_path.name.upper()
    if name.startswith("MNQ"):
        return "MNQ"
    if name.startswith("MES"):
        return "MES"
    if name.startswith("ES"):
        return "ES"
    return None


def matching_configs(symbol: str) -> list[str]:
    return [k for k, v in CONFIG_VARIANTS.items() if v["symbol"] == symbol and (ROOT / v["path"]).exists()]


def run_optimizations(skip_existing: bool = False, trials_override: int | None = None) -> list[dict[str, Any]]:
    ensure_optimize_net_pnl_configs()
    OPT_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    for spec in OPTIMIZATION_RUNS:
        result_path = OPT_DIR / spec["output_result"] / "optimization_result.json"
        trials = trials_override or spec["trials"]
        if skip_existing and result_path.exists():
            data = json.loads(result_path.read_text(encoding="utf-8"))
            data["run_id"] = spec["id"]
            data["instrument"] = spec["instrument"]
            data["base_config"] = spec["base_config"]
            data["data"] = spec["data"]
            results.append(data)
            print(f"SKIP opt {spec['id']} (existing)")
            continue

        base = ROOT / spec["base_config"]
        csv_path = ROOT / spec["data"]
        out_sub = OPT_DIR / spec["output_result"]
        out_sub.mkdir(parents=True, exist_ok=True)

        print(f"RUN opt {spec['id']}: focus={spec['focus']} trials={trials}")
        result = run_optimization(base, csv_path, n_trials=trials, focus=spec["focus"])
        result["run_id"] = spec["id"]
        result["instrument"] = spec["instrument"]
        result["base_config"] = spec["base_config"]
        result["data"] = spec["data"]
        result_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

        if spec.get("output_config"):
            apply_params_to_config(base, result["best_params"], ROOT / spec["output_config"])

        results.append(result)
        print(f"  best {result['metric']}={result['best_value']:.2f} trades={result['final_trades']}")

    return results


def run_backtest_row(config_key: str, data_path: Path, out_dir: Path) -> dict[str, Any]:
    cfg_rel = CONFIG_VARIANTS[config_key]["path"]
    cfg_path = ROOT / cfg_rel
    out_dir.mkdir(parents=True, exist_ok=True)
    row: dict[str, Any] = {
        "variant": config_key,
        "config": cfg_rel,
        "data": data_path.name,
        "data_path": str(data_path.relative_to(ROOT)) if data_path.is_relative_to(ROOT) else str(data_path),
        "category": _data_category(data_path),
    }
    try:
        result = run_backtest_from_paths(cfg_path, data_path)
        generate_report(result, out_dir)
        m = result.metrics
        row.update(
            {
                "trades": m.total_trades,
                "win_rate": m.win_rate,
                "net_pnl": m.net_pnl,
                "profit_factor": m.profit_factor,
                "max_drawdown": m.max_drawdown,
                "l2_approximated": result.l2_approximated,
                "error": None,
            }
        )
        summary = {
            "metrics": m.model_dump(),
            "variant": config_key,
            "data": data_path.name,
            "warnings": result.warnings,
            "l2_approximated": result.l2_approximated,
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"OK {config_key} x {data_path.name}: pnl={m.net_pnl:.2f} trades={m.total_trades}")
    except Exception as exc:
        row.update(
            {
                "trades": 0,
                "win_rate": 0.0,
                "net_pnl": 0.0,
                "profit_factor": 0.0,
                "max_drawdown": 0.0,
                "l2_approximated": False,
                "error": str(exc),
            }
        )
        print(f"FAIL {config_key} x {data_path.name}: {exc}")
    return row


def _data_category(path: Path) -> str:
    if "sample" in path.parts:
        return "sample"
    if COMBINED_PATTERN.match(path.name):
        return "combined"
    if DAY_PATTERN.match(path.name):
        return "per_day"
    return "other"


def run_all_backtests() -> list[dict[str, Any]]:
    data = discover_data()
    rows: list[dict[str, Any]] = []

    for symbol in ("MNQ", "MES"):
        variants = matching_configs(symbol)
        for data_path in data[symbol]:
            for variant in variants:
                out_name = f"{data_path.stem}__{variant}"
                out_dir = BACKTEST_DIR / out_name
                rows.append(run_backtest_row(variant, data_path, out_dir))

    (OUT_ROOT / "backtest_results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return rows


def fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_No data._\n"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    lines = ["| " + " | ".join(headers) + " |", sep]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines) + "\n"


def generate_report_md(opt_results: list[dict[str, Any]], bt_rows: list[dict[str, Any]], data_found: dict[str, list[Path]]) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    opt_table_rows = []
    for r in opt_results:
        fm = r.get("final_metrics", {})
        opt_table_rows.append(
            [
                r.get("run_id", ""),
                r.get("instrument", ""),
                r.get("focus", ""),
                r.get("base_config", ""),
                r.get("n_trials", ""),
                ", ".join(f"{k}={v}" for k, v in (r.get("best_params") or {}).items()),
                fmt_money(float(r.get("best_value", 0))),
                fm.get("total_trades", r.get("final_trades", 0)),
                fmt_pct(fm.get("win_rate", 0)),
                f"{fm.get('profit_factor', 0):.2f}",
                fmt_money(fm.get("max_drawdown", 0)),
            ]
        )

    combined_rows = [r for r in bt_rows if r.get("category") == "combined" and not r.get("error")]
    combined_rows.sort(key=lambda x: x.get("net_pnl", 0), reverse=True)

    top15 = sorted([r for r in bt_rows if not r.get("error")], key=lambda x: x.get("net_pnl", 0), reverse=True)[:15]

    def combined_for_variant(variant: str) -> dict[str, Any] | None:
        for r in combined_rows:
            if r["variant"] == variant and "combined" in r["data"]:
                return r
        return None

    mnq_exit = combined_for_variant("mnq_exit_optimized")
    mnq_default_c = combined_for_variant("mnq_default")
    mnq_full = combined_for_variant("mnq_full_optimized")
    mnq_entry_c = combined_for_variant("mnq_entry_optimized")

    def exit_compare_row(label: str, row: dict[str, Any] | None) -> list[str]:
        if not row:
            return [label, "—", "—", "—", "—", "—", "—"]
        return [
            label,
            str(row.get("trades", 0)),
            fmt_pct(row.get("win_rate", 0)),
            fmt_money(row.get("net_pnl", 0)),
            f"{row.get('profit_factor', 0):.2f}",
            fmt_money(row.get("max_drawdown", 0)),
            row.get("variant", ""),
        ]

    per_symbol: dict[str, list[dict[str, Any]]] = {"MNQ": [], "MES": []}
    for r in combined_rows:
        sym = CONFIG_VARIANTS.get(r["variant"], {}).get("symbol")
        if sym in per_symbol:
            per_symbol[sym].append(r)

    best_per_instrument = []
    for sym, rows in per_symbol.items():
        viable = [r for r in rows if r.get("trades", 0) >= 3]
        if not viable:
            viable = rows
        if viable:
            best = max(viable, key=lambda x: x.get("net_pnl", 0))
            best_per_instrument.append((sym, best))

    data_inventory = []
    for sym, paths in data_found.items():
        if paths:
            data_inventory.append(f"- **{sym}**: {len(paths)} files ({', '.join(p.name for p in paths[:3])}{'...' if len(paths) > 3 else ''})")

    recommended = []
    for sym, best in best_per_instrument:
        cfg_path = ROOT / CONFIG_VARIANTS[best["variant"]]["path"]
        if cfg_path.exists():
            snippet = cfg_path.read_text(encoding="utf-8")
            recommended.append(f"### {sym} — `{best['variant']}` (combined net PnL {fmt_money(best['net_pnl'])})\n\n```yaml\n{snippet.strip()}\n```")

    md = f"""# Variant Sweep Report

Generated: {ts}  
Output: `data/reports/variant_sweep/`

Research only — no live trading.

## Data inventory

{chr(10).join(data_inventory) if data_inventory else '_No bar data found._'}

**Symbols with usable 1m bar CSVs:** MNQ ({len(data_found.get('MNQ', []))} files), MES ({len(data_found.get('MES', []))} files).  
ES sample at `futures-options-backtest-lab/data/local/ES_20260501_sample.csv` is raw L2 ticks (not bar format) — skipped.

## A. Optimization results

{_table(
    ["Run", "Instrument", "Focus", "Base config", "Trials", "Best params", "Net PnL", "Trades", "Win %", "PF", "Max DD"],
    opt_table_rows,
)}

## B. Best config per instrument (combined data, min 3 trades preferred)

{_table(
    ["Instrument", "Variant", "Trades", "Win %", "Net PnL", "PF", "Max DD"],
    [
        [
            sym,
            b["variant"],
            b.get("trades", 0),
            fmt_pct(b.get("win_rate", 0)),
            fmt_money(b.get("net_pnl", 0)),
            f"{b.get('profit_factor', 0):.2f}",
            fmt_money(b.get("max_drawdown", 0)),
        ]
        for sym, b in best_per_instrument
    ],
)}

## C. Top 15 backtest runs (all symbols × variants)

{_table(
    ["Rank", "Variant", "Data", "Category", "Trades", "Win %", "Net PnL", "PF", "Max DD"],
    [
        [
            i + 1,
            r["variant"],
            r["data"],
            r.get("category", ""),
            r.get("trades", 0),
            fmt_pct(r.get("win_rate", 0)),
            fmt_money(r.get("net_pnl", 0)),
            f"{r.get('profit_factor', 0):.2f}",
            fmt_money(r.get("max_drawdown", 0)),
        ]
        for i, r in enumerate(top15)
    ],
)}

## D. Exit param comparison — MNQ on combined data

{_table(
    ["Config", "Trades", "Win %", "Net PnL", "PF", "Max DD", "Variant key"],
    [
        exit_compare_row("mnq_default", mnq_default_c),
        exit_compare_row("mnq_entry_optimized", mnq_entry_c),
        exit_compare_row("mnq_exit_optimized", mnq_exit),
        exit_compare_row("mnq_full_optimized", mnq_full),
    ],
)}

## E. Per-symbol summary (combined runs)

### MNQ

{_table(
    ["Variant", "Trades", "Win %", "Net PnL", "PF", "Max DD"],
    [
        [
            r["variant"],
            r.get("trades", 0),
            fmt_pct(r.get("win_rate", 0)),
            fmt_money(r.get("net_pnl", 0)),
            f"{r.get('profit_factor', 0):.2f}",
            fmt_money(r.get("max_drawdown", 0)),
        ]
        for r in sorted(per_symbol.get("MNQ", []), key=lambda x: x.get("net_pnl", 0), reverse=True)
    ],
)}

### MES

{_table(
    ["Variant", "Trades", "Win %", "Net PnL", "PF", "Max DD"],
    [
        [
            r["variant"],
            r.get("trades", 0),
            fmt_pct(r.get("win_rate", 0)),
            fmt_money(r.get("net_pnl", 0)),
            f"{r.get('profit_factor', 0):.2f}",
            fmt_money(r.get("max_drawdown", 0)),
        ]
        for r in sorted(per_symbol.get("MES", []), key=lambda x: x.get("net_pnl", 0), reverse=True)
    ],
)}

## F. Recommended production configs

{chr(10).join(recommended) if recommended else '_Run optimizations first._'}

## Sweep stats

- Optimization runs: {len(opt_results)}
- Backtest runs: {len(bt_rows)} ({sum(1 for r in bt_rows if not r.get('error'))} ok, {sum(1 for r in bt_rows if r.get('error'))} failed)
- Config variants: {len([k for k, v in CONFIG_VARIANTS.items() if (ROOT / v['path']).exists()])}
"""
    return md


def main() -> None:
    parser = argparse.ArgumentParser(description="Run full variant optimization + backtest sweep")
    parser.add_argument("--skip-opt", action="store_true", help="Skip optimization (use existing results)")
    parser.add_argument("--skip-backtest", action="store_true", help="Skip backtests")
    parser.add_argument("--trials", type=int, default=None, help="Override trial count for all optimizations")
    parser.add_argument("--report-only", action="store_true", help="Regenerate report from saved JSON only")
    args = parser.parse_args()

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    data_found = discover_data()

    if args.report_only:
        opt_path = OUT_ROOT / "optimization_results.json"
        bt_path = OUT_ROOT / "backtest_results.json"
        opt_results = json.loads(opt_path.read_text(encoding="utf-8")) if opt_path.exists() else []
        bt_rows = json.loads(bt_path.read_text(encoding="utf-8")) if bt_path.exists() else []
    else:
        opt_results = []
        if not args.skip_opt:
            opt_results = run_optimizations(skip_existing=False, trials_override=args.trials)
            (OUT_ROOT / "optimization_results.json").write_text(json.dumps(opt_results, indent=2, default=str), encoding="utf-8")
        else:
            opt_path = OUT_ROOT / "optimization_results.json"
            if opt_path.exists():
                opt_results = json.loads(opt_path.read_text(encoding="utf-8"))

        bt_rows = []
        if not args.skip_backtest:
            bt_rows = run_all_backtests()

    report = generate_report_md(opt_results, bt_rows, data_found)
    report_path = OUT_ROOT / "VARIANT_SWEEP_REPORT.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"Wrote {report_path}")


if __name__ == "__main__":
    main()

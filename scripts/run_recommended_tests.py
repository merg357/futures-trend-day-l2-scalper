#!/usr/bin/env python3
"""Run all recommended research tests for futures-trend-day-l2-scalper."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scalper.backtest import run_backtest, run_backtest_from_paths
from scalper.config import ScalperConfig, config_to_dict, load_config
from scalper.l2_etl import convert_archive_to_bars
from scalper.models import BacktestResult
from scalper.mtf_backtest import run_mtf_backtest, run_mtf_backtest_from_paths
from scalper.optimize import run_optimization

RAW = ROOT / "data" / "raw"
SUBMIN = RAW / "submin"
REPORT_DIR = ROOT / "data" / "reports" / "recommended_tests"
INVENTORY_PATH = Path(r"D:\AI_Vault\futures-options-backtest-lab\results\tradedata_inventory.json")
ARCHIVE_ROOT = Path(r"D:\TradeData\StorageBox\bundles\futuresbot\archives\l2")
SVG_EMPIRE_ROOT = Path(r"D:\svg-empire\TradeData")

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

DAY_PATTERN = re.compile(r"^(MNQ|MES)_(\d{8})_1m\.csv$", re.IGNORECASE)
TRAIN_DAYS = 15
HOLDOUT_DAYS = 7


def fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def list_instrument_1m_dates(instrument: str) -> list[str]:
    dates: set[str] = set()
    for path in RAW.glob(f"{instrument}_*_1m.csv"):
        m = DAY_PATTERN.match(path.name)
        if m and m.group(1).upper() == instrument.upper():
            dates.add(m.group(2))
    return sorted(dates)


def build_combined_csv(instrument: str, dates: list[str], out_path: Path | None = None) -> Path:
    parts = [RAW / f"{instrument}_{d}_1m.csv" for d in dates]
    missing = [str(p) for p in parts if not p.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing daily CSVs: {missing[:3]}...")
    frames = [pd.read_csv(p, parse_dates=["timestamp"]) for p in parts]
    combined = pd.concat(frames, ignore_index=True).sort_values("timestamp")
    if out_path is None:
        out_path = RAW / f"{instrument}_combined_1m.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(out_path, index=False)
    return out_path


def apply_params_to_config(base_path: Path, best_params: dict[str, Any], out_path: Path) -> None:
    cfg = load_config(base_path)
    for key, value in best_params.items():
        if key in PARAM_MAP:
            section, attr = PARAM_MAP[key]
            setattr(getattr(cfg, section), attr, value)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        yaml.dump(config_to_dict(cfg), fh, default_flow_style=False, sort_keys=False, allow_unicode=True)


def metrics_row(label: str, result: BacktestResult, *, config_name: str = "") -> dict[str, Any]:
    m = result.metrics
    cfg = load_config(result.config_path) if result.config_path else None
    row: dict[str, Any] = {
        "label": label,
        "config": config_name or result.config_path,
        "trades": m.total_trades,
        "win_rate": m.win_rate,
        "net_pnl": m.net_pnl,
        "profit_factor": m.profit_factor,
        "max_drawdown": m.max_drawdown,
        "avg_bars_held": m.avg_bars_held,
        "sharpe_ratio": m.sharpe_ratio,
    }
    if cfg:
        row["stop_loss_ticks"] = cfg.exit.stop_loss_ticks
    return row


def run_backtest_label(label: str, config_path: Path, data_path: Path) -> dict[str, Any]:
    result = run_backtest_from_paths(config_path, data_path)
    row = metrics_row(label, result, config_name=str(config_path.relative_to(ROOT)))
    return row


def run_mtf_label(label: str, config_path: Path, data_path: Path) -> dict[str, Any]:
    result = run_mtf_backtest_from_paths(config_path, data_path)
    row = metrics_row(label, result, config_name=str(config_path.relative_to(ROOT)))
    row["mode"] = "mtf_5m_1m"
    return row


def overfit_ratio(train_pnl: float, holdout_pnl: float) -> float | None:
    if train_pnl == 0:
        return None
    return holdout_pnl / train_pnl


def test_walk_forward(trials: int, force: bool) -> dict[str, Any]:
    print("\n=== Test 1: Walk-forward validation (MNQ 1m) ===")
    out_path = REPORT_DIR / "walk_forward_results.json"
    if out_path.is_file() and not force:
        return json.loads(out_path.read_text(encoding="utf-8"))

    dates = list_instrument_1m_dates("MNQ")
    if len(dates) < TRAIN_DAYS + HOLDOUT_DAYS:
        raise SystemExit(f"Need {TRAIN_DAYS + HOLDOUT_DAYS} MNQ days, found {len(dates)}")

    train_dates = dates[:TRAIN_DAYS]
    holdout_dates = dates[TRAIN_DAYS : TRAIN_DAYS + HOLDOUT_DAYS]
    train_csv = REPORT_DIR / "MNQ_train_1m.csv"
    holdout_csv = REPORT_DIR / "MNQ_holdout_1m.csv"
    build_combined_csv("MNQ", train_dates, train_csv)
    build_combined_csv("MNQ", holdout_dates, holdout_csv)

    base_config = ROOT / "configs" / "mnq_optimize_net_pnl.yaml"
    print(f"Optuna on train ({len(train_dates)} days, {trials} trials)...")
    opt = run_optimization(
        base_config,
        train_csv,
        n_trials=trials,
        focus="all",
        scalping=False,
    )

    wf_config = ROOT / "configs" / "mnq_walkforward_optimized.yaml"
    apply_params_to_config(base_config, opt["best_params"], wf_config)

    wf_cfg = load_config(wf_config)
    train_result = run_backtest(wf_cfg, train_csv, config_path=str(wf_config))
    holdout_result = run_backtest(wf_cfg, holdout_csv, config_path=str(wf_config))
    full_opt_result = run_backtest_from_paths(ROOT / "configs" / "mnq_full_optimized.yaml", holdout_csv)

    payload = {
        "train_dates": train_dates,
        "holdout_dates": holdout_dates,
        "n_trials": trials,
        "best_params": opt["best_params"],
        "optimized_config": str(wf_config.relative_to(ROOT)),
        "train": metrics_row("walkforward_optimized_train", train_result),
        "holdout_walkforward": metrics_row("walkforward_optimized_holdout", holdout_result),
        "holdout_full_optimized": metrics_row("mnq_full_optimized_holdout", full_opt_result),
        "overfit_ratio": overfit_ratio(train_result.metrics.net_pnl, holdout_result.metrics.net_pnl),
        "optuna_train_metrics": opt["final_metrics"],
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def test_sl_floor(trials: int, force: bool) -> dict[str, Any]:
    print("\n=== Test 2: MNQ SL floor >= 16 ticks ===")
    out_path = REPORT_DIR / "sl_floor_results.json"
    if out_path.is_file() and not force:
        return json.loads(out_path.read_text(encoding="utf-8"))

    data_path = RAW / "MNQ_combined_1m.csv"
    if not data_path.is_file():
        build_combined_csv("MNQ", list_instrument_1m_dates("MNQ"))

    base_config = ROOT / "configs" / "mnq_optimize_net_pnl.yaml"
    print(f"Optuna exit focus, SL 16-40, {trials} trials...")
    opt = run_optimization(
        base_config,
        data_path,
        n_trials=trials,
        focus="exit",
        stop_loss_min=16,
        stop_loss_max=40,
    )

    sl16_config = ROOT / "configs" / "mnq_sl16_optimized.yaml"
    apply_params_to_config(base_config, opt["best_params"], sl16_config)

    comparisons = [
        run_backtest_label("mnq_sl16_optimized", sl16_config, data_path),
        run_backtest_label("mnq_full_optimized_sl6", ROOT / "configs" / "mnq_full_optimized.yaml", data_path),
        run_backtest_label("mnq_default_sl10", ROOT / "configs" / "mnq_default.yaml", data_path),
    ]

    payload = {
        "n_trials": trials,
        "stop_loss_range": [16, 40],
        "focus": "exit",
        "best_params": opt["best_params"],
        "optimized_config": str(sl16_config.relative_to(ROOT)),
        "optimization_metrics": opt["final_metrics"],
        "comparisons": comparisons,
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def test_mtf(trials: int, force: bool) -> dict[str, Any]:
    print("\n=== Test 3: MTF 5m trend + 1m execution ===")
    out_path = REPORT_DIR / "mtf_results.json"
    if out_path.is_file() and not force:
        return json.loads(out_path.read_text(encoding="utf-8"))

    data_path = RAW / "MNQ_combined_1m.csv"
    if not data_path.is_file():
        build_combined_csv("MNQ", list_instrument_1m_dates("MNQ"))

    base_config = ROOT / "configs" / "mnq_mtf.yaml"
    baseline = run_mtf_label("mnq_mtf_baseline", base_config, data_path)
    single_1m = run_backtest_label("mnq_default_1m", ROOT / "configs" / "mnq_default.yaml", data_path)

    print(f"MTF Optuna ({trials} trials)...")
    opt = run_optimization(
        base_config,
        data_path,
        n_trials=trials,
        focus="all",
        scalping=False,
        backtest_fn=run_mtf_backtest,
    )

    mtf_opt_config = REPORT_DIR / "mnq_mtf_optimized.yaml"
    apply_params_to_config(base_config, opt["best_params"], mtf_opt_config)
    optimized = run_mtf_label("mnq_mtf_optimized", mtf_opt_config, data_path)

    payload = {
        "n_trials": trials,
        "trend_timeframe_minutes": 5,
        "execution_timeframe": "1m",
        "best_params": opt["best_params"],
        "optimized_config": str(mtf_opt_config.relative_to(ROOT)),
        "optimization_metrics": opt["final_metrics"],
        "comparisons": [baseline, single_1m, optimized],
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def load_mes_dates() -> list[str]:
    manifest = RAW / "etl_manifest.json"
    dates: set[str] = set()
    if manifest.is_file():
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        for f in payload.get("files", []):
            if f.get("instrument") == "MES" and f.get("interval", "1m") == "1m":
                dates.add(f["date"])
    for path in RAW.glob("MES_*_1m.csv"):
        m = re.match(r"^MES_(\d{8})_1m\.csv$", path.name, re.IGNORECASE)
        if m:
            dates.add(m.group(1))
    return sorted(dates)


def build_mes_combined_30s() -> Path:
    parts = sorted(SUBMIN.glob("MES_*_30s.csv"))
    if not parts:
        raise FileNotFoundError("No MES 30s CSVs in data/raw/submin/")
    frames = [pd.read_csv(p, parse_dates=["timestamp"]) for p in parts]
    combined = pd.concat(frames, ignore_index=True).sort_values("timestamp")
    out_path = SUBMIN / "MES_combined_30s.csv"
    combined.to_csv(out_path, index=False)
    return out_path


def run_mes_30s_etl(dates: list[str], skip_existing: bool = True) -> list[dict[str, Any]]:
    SUBMIN.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for date in dates:
        out_path = SUBMIN / f"MES_{date}_30s.csv"
        if skip_existing and out_path.is_file():
            continue
        try:
            result = convert_archive_to_bars("MES", date, output_dir=SUBMIN, interval="30s")
            results.append(result.to_dict())
            print(f"OK ETL MES {date} 30s -> {result.row_count} bars")
        except Exception as exc:  # noqa: BLE001
            print(f"ERR MES {date} 30s: {exc}")
    return results


def test_mes_30s(trials: int, force: bool) -> dict[str, Any]:
    print("\n=== Test 4: MES 30s sub-minute ===")
    out_path = REPORT_DIR / "mes_30s_results.json"
    if out_path.is_file() and not force:
        return json.loads(out_path.read_text(encoding="utf-8"))

    dates = load_mes_dates()
    if not dates:
        raise SystemExit("No MES 1m dates found")
    run_mes_30s_etl(dates, skip_existing=not force)
    data_30s = build_mes_combined_30s()

    data_1m = RAW / "MES_combined_1m.csv"
    if not data_1m.is_file():
        build_combined_csv("MES", dates)

    base_config = ROOT / "configs" / "mes_30s.yaml"
    baseline = run_backtest_label("mes_30s_baseline", base_config, data_30s)

    print(f"MES 30s Optuna ({trials} trials, scalping)...")
    opt = run_optimization(
        base_config,
        data_30s,
        n_trials=trials,
        focus="all",
        scalping=True,
    )

    mes_opt_config = ROOT / "configs" / "mes_30s_optimized.yaml"
    apply_params_to_config(base_config, opt["best_params"], mes_opt_config)
    optimized = run_backtest_label("mes_30s_optimized", mes_opt_config, data_30s)
    mes_1m_opt = run_backtest_label("mes_full_optimized_1m", ROOT / "configs" / "mes_full_optimized.yaml", data_1m)

    payload = {
        "mes_dates": dates,
        "n_trials": trials,
        "best_params": opt["best_params"],
        "optimized_config": str(mes_opt_config.relative_to(ROOT)),
        "optimization_metrics": opt["final_metrics"],
        "comparisons": [baseline, optimized, mes_1m_opt],
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def load_inventory_dates(instrument: str) -> list[str]:
    if not INVENTORY_PATH.is_file():
        return []
    payload = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    return sorted(payload.get("merged", {}).get(f"l2/{instrument}", {}).get("dates", []))


def load_manifest_1m_dates(instrument: str) -> set[str]:
    manifest = RAW / "etl_manifest.json"
    if not manifest.is_file():
        return set()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    return {
        f["date"]
        for f in payload.get("files", [])
        if f.get("instrument") == instrument and f.get("interval", "1m") == "1m"
    }


def load_manifest_errors() -> list[str]:
    manifest = RAW / "etl_manifest.json"
    if not manifest.is_file():
        return []
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    return list(payload.get("errors", []))


def try_vps_pull() -> dict[str, Any]:
    status: dict[str, Any] = {
        "host": "187.124.244.78",
        "attempted": True,
        "success": False,
        "message": "",
        "files_pulled": [],
    }
    ssh_cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "root@187.124.244.78",
        "ls /root/svg-empire/TradeData/archives/l2/MNQ 2>/dev/null | head -5 || echo VPS_NO_DATA",
    ]
    try:
        proc = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=20, check=False)
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0:
            status["message"] = f"SSH failed (rc={proc.returncode}): {stderr or stdout}"
            return status
        if "VPS_NO_DATA" in stdout or not stdout:
            status["message"] = "SSH OK but no TradeData archives found on VPS"
            status["success"] = True
            return status
        status["success"] = True
        status["message"] = f"VPS reachable; sample listing: {stdout.splitlines()[:3]}"
    except Exception as exc:  # noqa: BLE001
        status["message"] = str(exc)
    return status


def convert_local_archive_dates(instrument: str, dates: list[str]) -> tuple[list[str], list[str]]:
    added: list[str] = []
    errors: list[str] = []
    for date in dates:
        out_path = RAW / f"{instrument}_{date}_1m.csv"
        if out_path.is_file():
            continue
        try:
            result = convert_archive_to_bars(instrument, date, output_dir=RAW, interval="1m")
            added.append(date)
            print(f"OK local ETL {instrument} {date} 1m -> {result.row_count} bars")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{instrument} {date}: {exc}")
            print(f"ERR local ETL {instrument} {date}: {exc}")
    return added, errors


def update_etl_manifest(new_entries: list[dict[str, Any]], new_errors: list[str]) -> None:
    manifest_path = RAW / "etl_manifest.json"
    existing: dict[str, Any] = {"files": [], "errors": []}
    if manifest_path.is_file():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_key = {
        (f["instrument"], f["date"], f.get("interval", "1m")): f for f in existing.get("files", [])
    }
    for entry in new_entries:
        by_key[(entry["instrument"], entry["date"], entry.get("interval", "1m"))] = entry
    merged_files = sorted(by_key.values(), key=lambda x: (x["instrument"], x["date"], x.get("interval", "1m")))
    errors = sorted(set(existing.get("errors", []) + new_errors))
    manifest_path.write_text(
        json.dumps({"files": merged_files, "errors": errors}, indent=2),
        encoding="utf-8",
    )


def test_etl_expansion(force: bool) -> dict[str, Any]:
    print("\n=== Test 5: ETL expansion + VPS pull ===")
    out_path = REPORT_DIR / "etl_expansion.json"
    if out_path.is_file() and not force:
        return json.loads(out_path.read_text(encoding="utf-8"))

    prior_mnq = set(list_instrument_1m_dates("MNQ"))
    prior_errors = load_manifest_errors()
    vps_status = try_vps_pull()

    inventory_mnq = load_inventory_dates("MNQ")
    manifest_mnq = load_manifest_1m_dates("MNQ")
    pending_mnq = [d for d in inventory_mnq if d not in manifest_mnq and d not in prior_mnq]

    added_mnq, errors_mnq = convert_local_archive_dates("MNQ", pending_mnq)
    new_mnq_dates = sorted(set(list_instrument_1m_dates("MNQ")) - prior_mnq)

    baseline_before: dict[str, Any] | None = None
    baseline_after: dict[str, Any] | None = None
    if new_mnq_dates:
        combined = RAW / "MNQ_combined_1m.csv"
        if combined.is_file():
            baseline_before = run_backtest_label("mnq_default_before_expansion", ROOT / "configs" / "mnq_default.yaml", combined)
        build_combined_csv("MNQ", list_instrument_1m_dates("MNQ"))
        baseline_after = run_backtest_label("mnq_default_after_expansion", ROOT / "configs" / "mnq_default.yaml", combined)

    payload = {
        "vps_status": vps_status,
        "prior_manifest_errors": prior_errors,
        "pending_from_inventory": pending_mnq,
        "dates_added_mnq": new_mnq_dates,
        "etl_errors": errors_mnq,
        "svg_empire_root_checked": str(SVG_EMPIRE_ROOT),
        "archive_root": str(ARCHIVE_ROOT),
        "baseline_before": baseline_before,
        "baseline_after": baseline_after,
        "total_mnq_days": len(list_instrument_1m_dates("MNQ")),
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def generate_report_md(
    wf: dict[str, Any],
    sl: dict[str, Any],
    mtf: dict[str, Any],
    mes: dict[str, Any],
    etl: dict[str, Any],
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Recommended Tests Report",
        "",
        f"Generated: {now}",
        "",
        "Research only — not for live trading.",
        "",
        "## A. Walk-forward validation (MNQ 1m)",
        "",
        f"- Train days ({TRAIN_DAYS}): `{', '.join(wf.get('train_dates', []))}`",
        f"- Holdout days ({HOLDOUT_DAYS}): `{', '.join(wf.get('holdout_dates', []))}`",
        f"- Optuna trials: {wf.get('n_trials', 0)} (train only, metric net_pnl)",
        "",
        "| Split | Config | Trades | Win % | Net PnL | PF | Max DD | Overfit ratio |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]

    train = wf.get("train", {})
    holdout_wf = wf.get("holdout_walkforward", {})
    holdout_full = wf.get("holdout_full_optimized", {})
    ratio = wf.get("overfit_ratio")
    ratio_str = f"{ratio:.2f}" if ratio is not None else "—"

    lines.append(
        f"| Train | walkforward optimized | {train.get('trades', 0)} | "
        f"{fmt_pct(train.get('win_rate', 0))} | {fmt_money(train.get('net_pnl', 0))} | "
        f"{train.get('profit_factor', 0):.2f} | {fmt_money(train.get('max_drawdown', 0))} | — |"
    )
    lines.append(
        f"| Holdout | walkforward optimized | {holdout_wf.get('trades', 0)} | "
        f"{fmt_pct(holdout_wf.get('win_rate', 0))} | {fmt_money(holdout_wf.get('net_pnl', 0))} | "
        f"{holdout_wf.get('profit_factor', 0):.2f} | {fmt_money(holdout_wf.get('max_drawdown', 0))} | {ratio_str} |"
    )
    lines.append(
        f"| Holdout | mnq_full_optimized (fixed) | {holdout_full.get('trades', 0)} | "
        f"{fmt_pct(holdout_full.get('win_rate', 0))} | {fmt_money(holdout_full.get('net_pnl', 0))} | "
        f"{holdout_full.get('profit_factor', 0):.2f} | {fmt_money(holdout_full.get('max_drawdown', 0))} | — |"
    )

    lines.extend(
        [
            "",
            "## B. MNQ SL floor >= 16 ticks",
            "",
            f"- Optimization: {sl.get('n_trials', 0)} trials, focus `{sl.get('focus')}`, SL range {sl.get('stop_loss_range')}",
            f"- Config: `{sl.get('optimized_config', '')}`",
            "",
            "| Config | SL ticks | Trades | Win % | Net PnL | PF | Max DD | Avg hold (bars) |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in sl.get("comparisons", []):
        lines.append(
            f"| {row.get('label')} | {row.get('stop_loss_ticks', '—')} | {row.get('trades', 0)} | "
            f"{fmt_pct(row.get('win_rate', 0))} | {fmt_money(row.get('net_pnl', 0))} | "
            f"{row.get('profit_factor', 0):.2f} | {fmt_money(row.get('max_drawdown', 0))} | "
            f"{row.get('avg_bars_held', 0):.1f} |"
        )

    lines.extend(
        [
            "",
            "## C. MTF 5m trend + 1m execution",
            "",
            f"- Trend filter: {mtf.get('trend_timeframe_minutes', 5)}m bars",
            f"- Execution: {mtf.get('execution_timeframe', '1m')}",
            f"- Optuna trials: {mtf.get('n_trials', 0)}",
            "",
            "| Mode | Config | Trades | Win % | Net PnL | PF | Max DD |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in mtf.get("comparisons", []):
        lines.append(
            f"| {row.get('mode', '1m')} | {row.get('label')} | {row.get('trades', 0)} | "
            f"{fmt_pct(row.get('win_rate', 0))} | {fmt_money(row.get('net_pnl', 0))} | "
            f"{row.get('profit_factor', 0):.2f} | {fmt_money(row.get('max_drawdown', 0))} |"
        )

    lines.extend(
        [
            "",
            "## D. MES 30s sub-minute",
            "",
            f"- MES dates: {len(mes.get('mes_dates', []))}",
            f"- Optuna trials: {mes.get('n_trials', 0)} (scalping mode)",
            f"- Config: `{mes.get('optimized_config', '')}`",
            "",
            "| Timeframe | Config | Trades | Win % | Net PnL | PF | Max DD |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in mes.get("comparisons", []):
        tf = "30s" if "30s" in row.get("label", "") else "1m"
        lines.append(
            f"| {tf} | {row.get('label')} | {row.get('trades', 0)} | "
            f"{fmt_pct(row.get('win_rate', 0))} | {fmt_money(row.get('net_pnl', 0))} | "
            f"{row.get('profit_factor', 0):.2f} | {fmt_money(row.get('max_drawdown', 0))} |"
        )

    vps = etl.get("vps_status", {})
    lines.extend(
        [
            "",
            "## E. ETL expansion",
            "",
            f"- VPS host: `{vps.get('host', '')}` — success: **{vps.get('success')}**",
            f"- VPS message: {vps.get('message', '—')}",
            f"- MNQ dates added: {len(etl.get('dates_added_mnq', []))} "
            f"({', '.join(etl.get('dates_added_mnq', [])) or 'none'})",
            f"- Total MNQ 1m days: {etl.get('total_mnq_days', 0)}",
            f"- Pending from inventory: {len(etl.get('pending_from_inventory', []))}",
            "",
        ]
    )
    if etl.get("baseline_before") and etl.get("baseline_after"):
        b = etl["baseline_before"]
        a = etl["baseline_after"]
        lines.extend(
            [
                "### MNQ baseline impact (mnq_default on combined 1m)",
                "",
                "| Stage | Trades | Net PnL | Win % |",
                "| --- | ---: | ---: | ---: |",
                f"| Before expansion | {b.get('trades', 0)} | {fmt_money(b.get('net_pnl', 0))} | {fmt_pct(b.get('win_rate', 0))} |",
                f"| After expansion | {a.get('trades', 0)} | {fmt_money(a.get('net_pnl', 0))} | {fmt_pct(a.get('win_rate', 0))} |",
                "",
            ]
        )
    else:
        lines.append("No new MNQ days added — baseline backtest unchanged.\n")

    lines.extend(
        [
            "## Caveats",
            "",
            "1. Walk-forward uses chronological split; overfit ratio = holdout PnL / train PnL.",
            "2. SL floor test optimizes exits only; tight-stop configs (SL=6/10) are fixed baselines.",
            "3. MTF uses 5m indicators aligned via merge_asof; L2/execution remain on 1m.",
            "4. MES 30s uses scalping Optuna ranges; compare to 1m optimized on different bar interval.",
            "5. Limited date range — validate on additional out-of-sample data before any deployment.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run recommended research tests")
    parser.add_argument("--trials-wf", type=int, default=70, help="Walk-forward Optuna trials")
    parser.add_argument("--trials-sl", type=int, default=70, help="SL floor Optuna trials")
    parser.add_argument("--trials-mtf", type=int, default=50, help="MTF Optuna trials")
    parser.add_argument("--trials-mes", type=int, default=70, help="MES 30s Optuna trials")
    parser.add_argument("--force", action="store_true", help="Re-run even if JSON outputs exist")
    parser.add_argument("--skip", nargs="+", choices=["1", "2", "3", "4", "5"], help="Skip test numbers")
    parser.add_argument("--pytest", action="store_true", help="Run pytest after tests")
    args = parser.parse_args()

    skip = set(args.skip or [])
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    wf: dict[str, Any] = {}
    sl: dict[str, Any] = {}
    mtf: dict[str, Any] = {}
    mes: dict[str, Any] = {}
    etl: dict[str, Any] = {}

    if "1" not in skip:
        wf = test_walk_forward(args.trials_wf, args.force)
    elif (REPORT_DIR / "walk_forward_results.json").is_file():
        wf = json.loads((REPORT_DIR / "walk_forward_results.json").read_text(encoding="utf-8"))

    if "2" not in skip:
        sl = test_sl_floor(args.trials_sl, args.force)
    elif (REPORT_DIR / "sl_floor_results.json").is_file():
        sl = json.loads((REPORT_DIR / "sl_floor_results.json").read_text(encoding="utf-8"))

    if "3" not in skip:
        mtf = test_mtf(args.trials_mtf, args.force)
    elif (REPORT_DIR / "mtf_results.json").is_file():
        mtf = json.loads((REPORT_DIR / "mtf_results.json").read_text(encoding="utf-8"))

    if "4" not in skip:
        mes = test_mes_30s(args.trials_mes, args.force)
    elif (REPORT_DIR / "mes_30s_results.json").is_file():
        mes = json.loads((REPORT_DIR / "mes_30s_results.json").read_text(encoding="utf-8"))

    if "5" not in skip:
        etl = test_etl_expansion(args.force)
    elif (REPORT_DIR / "etl_expansion.json").is_file():
        etl = json.loads((REPORT_DIR / "etl_expansion.json").read_text(encoding="utf-8"))

    report_md = generate_report_md(wf, sl, mtf, mes, etl)
    report_path = REPORT_DIR / "RECOMMENDED_TESTS_REPORT.md"
    report_path.write_text(report_md, encoding="utf-8")
    print(f"\nReport written to {report_path}")
    print("\n" + report_md)

    if args.pytest:
        print("\nRunning pytest...")
        proc = subprocess.run(
            [sys.executable, "-m", "pytest"],
            cwd=ROOT,
            check=False,
        )
        if proc.returncode != 0:
            raise SystemExit(proc.returncode)


if __name__ == "__main__":
    main()

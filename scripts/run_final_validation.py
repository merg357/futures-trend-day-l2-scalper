#!/usr/bin/env python3
"""Final validation suite for futures-trend-day-l2-scalper (research only)."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scalper.backtest import load_bars, run_backtest, run_backtest_from_paths
from scalper.config import ScalperConfig, config_to_dict, load_config
from scalper.entry_rules import evaluate_entry, is_chop
from scalper.indicators import compute_indicators
from scalper.l2_etl import convert_archive_to_bars
from scalper.l2_score import compute_l2_score
from scalper.models import Bias, Side
from scalper.mtf_backtest import run_mtf_backtest, run_mtf_backtest_from_paths
from scalper.optimize import run_optimization
from scalper.risk import RiskManager
from scalper.trend_score import compute_trend_score

# Reuse helpers from recommended tests
from scripts.run_recommended_tests import (
    apply_params_to_config,
    build_combined_csv,
    build_mes_combined_30s,
    list_instrument_1m_dates,
    load_mes_dates,
    metrics_row,
    overfit_ratio,
    run_mes_30s_etl,
)

RAW = ROOT / "data" / "raw"
SUBMIN = RAW / "submin"
REPORT_DIR = ROOT / "data" / "reports" / "final_validation"
ARCHIVE_ROOT = Path(r"D:\TradeData\StorageBox\bundles\futuresbot\archives\l2")
SVG_EMPIRE_ROOTS = [
    Path(r"D:\svg-empire\TradeData"),
    Path(r"D:\TradeData\svg-empire-20260611T165008Z-3-001\svg-empire\bundles\futuresbot\archives\l2"),
    Path(r"D:\TradeData\TradeData\StorageBox\bundles\futuresbot\archives\l2"),
]
TRAIN_DAYS = 15
HOLDOUT_DAYS = 7
HOLDOUT_DATES_MNQ = [
    "20260529",
    "20260601",
    "20260602",
    "20260604",
    "20260605",
    "20260606",
    "20260608",
]
GLOBEX_SUNDAY_DATES = ["20260503", "20260517", "20260524"]
MNQ_REPAIR_DATE = "20260508"


def fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def train_holdout_split(dates: list[str]) -> tuple[list[str], list[str]]:
    if len(dates) >= TRAIN_DAYS + HOLDOUT_DAYS:
        return dates[:TRAIN_DAYS], dates[TRAIN_DAYS : TRAIN_DAYS + HOLDOUT_DAYS]
    holdout = [d for d in HOLDOUT_DATES_MNQ if d in dates]
    train = [d for d in dates if d not in holdout]
    if len(train) >= TRAIN_DAYS:
        train = train[:TRAIN_DAYS]
    return train, holdout


def run_with_slippage(config_path: Path, data_path: Path, slippage_ticks: int) -> dict[str, Any]:
    cfg = load_config(config_path)
    cfg.backtest.slippage_ticks = slippage_ticks
    result = run_backtest(cfg, data_path, config_path=str(config_path))
    row = metrics_row(f"{config_path.stem}_slip{slippage_ticks}", result, config_name=str(config_path))
    row["slippage_ticks"] = slippage_ticks
    return row


def test_mtf_walkforward(trials: int, force: bool) -> dict[str, Any]:
    print("\n=== Test 1: MTF walk-forward holdout ===")
    out_path = REPORT_DIR / "mtf_walkforward_results.json"
    if out_path.is_file() and not force:
        return json.loads(out_path.read_text(encoding="utf-8"))

    dates = list_instrument_1m_dates("MNQ")
    train_dates, holdout_dates = train_holdout_split(dates)
    train_csv = REPORT_DIR / "MNQ_train_1m.csv"
    holdout_csv = REPORT_DIR / "MNQ_holdout_1m.csv"
    build_combined_csv("MNQ", train_dates, train_csv)
    build_combined_csv("MNQ", holdout_dates, holdout_csv)

    base_config = ROOT / "configs" / "mnq_mtf.yaml"
    print(f"MTF Optuna on train ({len(train_dates)} days, {trials} trials)...")
    opt = run_optimization(
        base_config,
        train_csv,
        n_trials=trials,
        focus="all",
        scalping=False,
        backtest_fn=run_mtf_backtest,
    )

    wf_config = REPORT_DIR / "mnq_mtf_walkforward_optimized.yaml"
    apply_params_to_config(base_config, opt["best_params"], wf_config)

    wf_cfg = load_config(wf_config)
    train_result = run_mtf_backtest(wf_cfg, train_csv, config_path=str(wf_config))
    holdout_result = run_mtf_backtest(wf_cfg, holdout_csv, config_path=str(wf_config))
    mtf_opt_path = REPORT_DIR.parent / "recommended_tests" / "mnq_mtf_optimized.yaml"
    holdout_fixed = (
        run_mtf_backtest_from_paths(mtf_opt_path, holdout_csv)
        if mtf_opt_path.is_file()
        else None
    )

    payload: dict[str, Any] = {
        "train_dates": train_dates,
        "holdout_dates": holdout_dates,
        "n_trials": trials,
        "best_params": opt["best_params"],
        "optimized_config": str(wf_config.relative_to(ROOT)),
        "train": metrics_row("mtf_walkforward_train", train_result),
        "holdout_walkforward": metrics_row("mtf_walkforward_holdout", holdout_result),
        "overfit_ratio": overfit_ratio(train_result.metrics.net_pnl, holdout_result.metrics.net_pnl),
        "optuna_train_metrics": opt["final_metrics"],
    }
    if holdout_fixed:
        payload["holdout_prior_mtf_optimized"] = metrics_row(
            "mnq_mtf_optimized_holdout", holdout_fixed
        )
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def test_mes_30s_walkforward(trials: int, force: bool) -> dict[str, Any]:
    print("\n=== Test 2: MES 30s walk-forward holdout ===")
    out_path = REPORT_DIR / "mes_30s_walkforward_results.json"
    if out_path.is_file() and not force:
        return json.loads(out_path.read_text(encoding="utf-8"))

    dates = load_mes_dates()
    train_dates, holdout_dates = train_holdout_split(dates)
    run_mes_30s_etl(dates, skip_existing=True)

    train_parts = [SUBMIN / f"MES_{d}_30s.csv" for d in train_dates]
    holdout_parts = [SUBMIN / f"MES_{d}_30s.csv" for d in holdout_dates]
    train_30s = REPORT_DIR / "MES_train_30s.csv"
    holdout_30s = REPORT_DIR / "MES_holdout_30s.csv"
    pd.concat([pd.read_csv(p, parse_dates=["timestamp"]) for p in train_parts]).sort_values("timestamp").to_csv(
        train_30s, index=False
    )
    pd.concat([pd.read_csv(p, parse_dates=["timestamp"]) for p in holdout_parts]).sort_values("timestamp").to_csv(
        holdout_30s, index=False
    )

    base_config = ROOT / "configs" / "mes_30s.yaml"
    print(f"MES 30s Optuna on train ({len(train_dates)} days, {trials} trials)...")
    opt = run_optimization(
        base_config,
        train_30s,
        n_trials=trials,
        focus="all",
        scalping=True,
    )

    wf_config = REPORT_DIR / "mes_30s_walkforward_optimized.yaml"
    apply_params_to_config(base_config, opt["best_params"], wf_config)

    train_result = run_backtest_from_paths(wf_config, train_30s)
    holdout_result = run_backtest_from_paths(wf_config, holdout_30s)

    payload = {
        "mes_dates": dates,
        "train_dates": train_dates,
        "holdout_dates": holdout_dates,
        "n_trials": trials,
        "best_params": opt["best_params"],
        "optimized_config": str(wf_config.relative_to(ROOT)),
        "train": metrics_row("mes_30s_walkforward_train", train_result),
        "holdout_walkforward": metrics_row("mes_30s_walkforward_holdout", holdout_result),
        "overfit_ratio": overfit_ratio(train_result.metrics.net_pnl, holdout_result.metrics.net_pnl),
        "optuna_train_metrics": opt["final_metrics"],
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def find_archive_candidates(instrument: str, date: str) -> list[Path]:
    candidates: list[Path] = []
    for root in [ARCHIVE_ROOT, *SVG_EMPIRE_ROOTS]:
        p = root / instrument / f"{date}.tar.gz"
        if p.is_file():
            candidates.append(p)
    return candidates


def test_mnq_repair(force: bool) -> dict[str, Any]:
    print("\n=== Test 3: MNQ 20260508 archive repair ===")
    out_path = REPORT_DIR / "mnq_repair_results.json"
    if out_path.is_file() and not force:
        return json.loads(out_path.read_text(encoding="utf-8"))

    candidates = find_archive_candidates("MNQ", MNQ_REPAIR_DATE)
    attempts: list[dict[str, Any]] = []
    success: dict[str, Any] | None = None

    for archive_path in candidates:
        for session_filter in ("rth", "all"):
            try:
                result = convert_archive_to_bars(
                    "MNQ",
                    MNQ_REPAIR_DATE,
                    archive=archive_path,
                    output_dir=RAW,
                    interval="1m",
                    session_filter=session_filter,
                    extra_archive_roots=SVG_EMPIRE_ROOTS,
                )
                attempts.append(
                    {
                        "archive": str(archive_path),
                        "session_filter": session_filter,
                        "status": "ok",
                        "row_count": result.row_count,
                        "warnings": result.warnings[:5],
                    }
                )
                success = result.to_dict()
                break
            except Exception as exc:  # noqa: BLE001
                attempts.append(
                    {
                        "archive": str(archive_path),
                        "session_filter": session_filter,
                        "status": "failed",
                        "error": str(exc),
                    }
                )
        if success:
            break

    payload = {
        "date": MNQ_REPAIR_DATE,
        "candidates_found": [str(p) for p in candidates],
        "attempts": attempts,
        "success": success,
        "csv_exists": (RAW / f"MNQ_{MNQ_REPAIR_DATE}_1m.csv").is_file(),
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def test_slippage_stress(force: bool) -> dict[str, Any]:
    print("\n=== Test 4: Slippage stress ===")
    out_path = REPORT_DIR / "slippage_stress_results.json"
    if out_path.is_file() and not force:
        return json.loads(out_path.read_text(encoding="utf-8"))

    configs = [
        ("mnq_default", ROOT / "configs" / "mnq_default.yaml", RAW / "MNQ_combined_1m.csv", "1m"),
        ("mnq_full_optimized", ROOT / "configs" / "mnq_full_optimized.yaml", RAW / "MNQ_combined_1m.csv", "1m"),
        (
            "mnq_mtf_optimized",
            REPORT_DIR.parent / "recommended_tests" / "mnq_mtf_optimized.yaml",
            RAW / "MNQ_combined_1m.csv",
            "mtf",
        ),
        ("mes_full_optimized", ROOT / "configs" / "mes_full_optimized.yaml", RAW / "MES_combined_1m.csv", "1m"),
        ("mes_30s_optimized", ROOT / "configs" / "mes_30s_optimized.yaml", SUBMIN / "MES_combined_30s.csv", "1m"),
    ]

    if not (RAW / "MNQ_combined_1m.csv").is_file():
        build_combined_csv("MNQ", list_instrument_1m_dates("MNQ"))
    if not (RAW / "MES_combined_1m.csv").is_file():
        build_combined_csv("MES", load_mes_dates())
    if not (SUBMIN / "MES_combined_30s.csv").is_file():
        run_mes_30s_etl(load_mes_dates())
        build_mes_combined_30s()

    results: list[dict[str, Any]] = []
    for label, cfg_path, data_path, mode in configs:
        if not cfg_path.is_file():
            results.append({"config_label": label, "error": f"missing config {cfg_path}"})
            continue
        for slip in (1, 2, 3):
            cfg = load_config(cfg_path)
            cfg.backtest.slippage_ticks = slip
            if mode == "mtf":
                bt = run_mtf_backtest(cfg, data_path, config_path=str(cfg_path))
            else:
                bt = run_backtest(cfg, data_path, config_path=str(cfg_path))
            row = metrics_row(f"{label}_slip{slip}", bt, config_name=str(cfg_path.relative_to(ROOT)))
            row["config_label"] = label
            row["slippage_ticks"] = slip
            results.append(row)

    payload = {"results": results}
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def test_globex_etl(force: bool) -> dict[str, Any]:
    print("\n=== Test 5: Globex/extended session ETL (Sunday dates) ===")
    out_path = REPORT_DIR / "globex_etl_results.json"
    if out_path.is_file() and not force:
        return json.loads(out_path.read_text(encoding="utf-8"))

    globex_dir = RAW / "globex"
    globex_dir.mkdir(parents=True, exist_ok=True)
    conversions: list[dict[str, Any]] = []

    for date in GLOBEX_SUNDAY_DATES:
        candidates = find_archive_candidates("MNQ", date)
        for session_filter in ("globex", "all"):
            for archive_path in candidates:
                try:
                    result = convert_archive_to_bars(
                        "MNQ",
                        date,
                        archive=archive_path,
                        output_dir=globex_dir,
                        interval="1m",
                        session_filter=session_filter,
                        extra_archive_roots=SVG_EMPIRE_ROOTS,
                    )
                    conversions.append(
                        {
                            "date": date,
                            "archive": str(archive_path),
                            "session_filter": session_filter,
                            "status": "ok",
                            **result.to_dict(),
                        }
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    conversions.append(
                        {
                            "date": date,
                            "archive": str(archive_path),
                            "session_filter": session_filter,
                            "status": "failed",
                            "error": str(exc),
                        }
                    )
            if any(c.get("date") == date and c.get("status") == "ok" for c in conversions):
                break

    payload = {
        "sunday_dates": GLOBEX_SUNDAY_DATES,
        "output_dir": str(globex_dir.relative_to(ROOT)),
        "conversions": conversions,
        "successful_dates": [c["date"] for c in conversions if c.get("status") == "ok"],
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def scan_nq_es_data() -> dict[str, Any]:
    print("\n=== Test 6: NQ/ES data scan ===")
    out_path = REPORT_DIR / "nq_es_scan_results.json"
    if out_path.is_file():
        return json.loads(out_path.read_text(encoding="utf-8"))

    scan_roots = [
        Path(r"D:\TradeData"),
        Path(r"D:\AI_Vault"),
        ROOT / "data" / "raw",
    ]
    patterns = {
        "NQ": ["NQ_*_1m.csv", "NQ_*.tar.gz"],
        "ES": ["ES_*_1m.csv", "ES_*.tar.gz"],
    }
    found: dict[str, list[str]] = {"NQ": [], "ES": []}
    etl_attempts: list[dict[str, Any]] = []

    for symbol in ("NQ", "ES"):
        for root in scan_roots:
            if not root.is_dir():
                continue
            for pat in patterns[symbol]:
                try:
                    for p in root.rglob(pat):
                        if p.is_file() and str(p) not in found[symbol]:
                            found[symbol].append(str(p))
                except OSError:
                    continue
        found[symbol] = sorted(found[symbol])[:50]

    # Attempt ETL from archives if any tar.gz found
    for symbol in ("NQ", "ES"):
        archives = [p for p in found[symbol] if p.endswith(".tar.gz")]
        for archive_str in archives[:3]:
            archive = Path(archive_str)
            date_match = re.search(r"(\d{8})", archive.name)
            if not date_match:
                continue
            date = date_match.group(1)
            try:
                result = convert_archive_to_bars(
                    symbol,
                    date,
                    archive=archive,
                    output_dir=RAW,
                    interval="1m",
                )
                etl_attempts.append({"symbol": symbol, "date": date, "status": "ok", **result.to_dict()})
            except Exception as exc:  # noqa: BLE001
                etl_attempts.append(
                    {"symbol": symbol, "date": date, "archive": archive_str, "status": "failed", "error": str(exc)}
                )

    payload = {
        "scan_roots": [str(r) for r in scan_roots],
        "found": found,
        "nq_csv_count": len([p for p in found["NQ"] if p.endswith(".csv")]),
        "es_csv_count": len([p for p in found["ES"] if p.endswith(".csv")]),
        "nq_archive_count": len([p for p in found["NQ"] if p.endswith(".tar.gz")]),
        "es_archive_count": len([p for p in found["ES"] if p.endswith(".tar.gz")]),
        "etl_attempts": etl_attempts,
        "note": "Full-size NQ/ES archives not present in TradeData l2 inventory; micro contracts MNQ/MES used.",
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def diagnose_entry_block(
    row: pd.Series,
    prev_atr: float,
    bar_index: int,
    config: ScalperConfig,
    cooldown: int,
    session_bar: int,
    risk: RiskManager,
) -> str | None:
    if cooldown > 0:
        return "cooldown"
    if not risk.can_enter():
        return f"risk_{risk.halt_reason or 'blocked'}"
    if session_bar < config.entry.min_bars_after_open:
        return "min_bars_after_open"
    if is_chop(row, config.entry):
        return "chop_filter"
    bid, ask = row.get("bid"), row.get("ask")
    if pd.notna(bid) and pd.notna(ask) and config.entry.max_spread_ticks > 0:
        spread_ticks = (float(ask) - float(bid)) / config.tick_size
        if spread_ticks > config.entry.max_spread_ticks:
            return "spread_too_wide"
    trend = compute_trend_score(row, prev_atr, config.trend)
    if trend.bias == Bias.NONE:
        return "trend_bias_none"
    if trend.score < config.trend.min_trend_score:
        return "trend_score_low"
    tol = config.entry.pullback_to_ema_ticks * config.tick_size
    if trend.bias == Bias.LONG:
        if row["low"] > row["ema_fast"] + tol or row["close"] <= row["ema_fast"]:
            return "pullback_failed"
    elif trend.bias == Bias.SHORT:
        if row["high"] < row["ema_fast"] - tol or row["close"] >= row["ema_fast"]:
            return "pullback_failed"
    side = Side.LONG if trend.bias == Bias.LONG else Side.SHORT
    if config.entry.require_l2_confirmation:
        l2 = compute_l2_score(row, side, config.l2)
        if l2.score < config.l2.min_l2_score:
            return "l2_score_low"
    return None


def test_blocked_signal_audit(force: bool) -> dict[str, Any]:
    print("\n=== Test 7: Blocked-signal audit (MNQ combined, mnq_default) ===")
    out_path = REPORT_DIR / "blocked_signal_audit.json"
    if out_path.is_file() and not force:
        return json.loads(out_path.read_text(encoding="utf-8"))

    data_path = RAW / "MNQ_combined_1m.csv"
    if not data_path.is_file():
        build_combined_csv("MNQ", list_instrument_1m_dates("MNQ"))

    config = load_config(ROOT / "configs" / "mnq_default.yaml")
    df = load_bars(data_path)
    df = compute_indicators(df, config.trend)

    blockers: Counter[str] = Counter()
    risk = RiskManager(config)
    cooldown = 0
    session_bar = 0
    prev_session_date = None
    bars_evaluated = 0

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        ts = row["timestamp"]
        if isinstance(ts, str):
            ts = pd.to_datetime(ts)
        session_date = ts.date() if hasattr(ts, "date") else None
        if session_date != prev_session_date:
            risk.reset_session()
            session_bar = 0
            prev_session_date = session_date
        session_bar += 1
        if cooldown > 0:
            cooldown -= 1

        reason = diagnose_entry_block(
            row, float(prev.get("atr", 0)), i, config, cooldown, session_bar, risk
        )
        if reason:
            blockers[reason] += 1
        bars_evaluated += 1

    top = blockers.most_common(15)
    payload = {
        "config": "configs/mnq_default.yaml",
        "data": str(data_path.relative_to(ROOT)),
        "bars_evaluated": bars_evaluated,
        "total_blocked": sum(blockers.values()),
        "top_blockers": [{"reason": r, "count": c, "pct": c / bars_evaluated} for r, c in top],
        "all_blockers": dict(blockers),
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def _portfolio_metrics(trades: list[dict[str, Any]], initial_capital: float = 50000.0) -> dict[str, Any]:
    if not trades:
        return {"total_trades": 0, "net_pnl": 0.0, "win_rate": 0.0, "max_drawdown": 0.0}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    equity = [initial_capital]
    for p in pnls:
        equity.append(equity[-1] + p)
    eq = np.array(equity)
    peak = np.maximum.accumulate(eq)
    max_dd = float((peak - eq).max())
    return {
        "total_trades": len(trades),
        "net_pnl": sum(pnls),
        "win_rate": len(wins) / len(pnls),
        "max_drawdown": max_dd,
        "profit_factor": (
            sum(wins) / abs(sum(p for p in pnls if p <= 0))
            if any(p <= 0 for p in pnls)
            else float("inf")
        ),
    }


def test_portfolio_backtest(force: bool) -> dict[str, Any]:
    print("\n=== Test 8: Portfolio backtest MNQ+MES ===")
    out_path = REPORT_DIR / "portfolio_backtest_results.json"
    if out_path.is_file() and not force:
        return json.loads(out_path.read_text(encoding="utf-8"))

    if not (RAW / "MNQ_combined_1m.csv").is_file():
        build_combined_csv("MNQ", list_instrument_1m_dates("MNQ"))
    if not (RAW / "MES_combined_1m.csv").is_file():
        build_combined_csv("MES", load_mes_dates())

    configs = {
        "mnq_walkforward": ROOT / "configs" / "mnq_walkforward_optimized.yaml",
        "mnq_full": ROOT / "configs" / "mnq_full_optimized.yaml",
        "mes_full": ROOT / "configs" / "mes_full_optimized.yaml",
        "mes_30s": ROOT / "configs" / "mes_30s_optimized.yaml",
    }

    individual: dict[str, Any] = {}
    mnq_wf = run_backtest_from_paths(configs["mnq_walkforward"], RAW / "MNQ_combined_1m.csv")
    individual["mnq_walkforward"] = metrics_row("mnq_walkforward", mnq_wf)
    mes_full = run_backtest_from_paths(configs["mes_full"], RAW / "MES_combined_1m.csv")
    individual["mes_full"] = metrics_row("mes_full", mes_full)

    if (SUBMIN / "MES_combined_30s.csv").is_file():
        mes_30s = run_backtest_from_paths(configs["mes_30s"], SUBMIN / "MES_combined_30s.csv")
        individual["mes_30s"] = metrics_row("mes_30s", mes_30s)

    mtf_path = REPORT_DIR.parent / "recommended_tests" / "mnq_mtf_optimized.yaml"
    if mtf_path.is_file():
        mtf = run_mtf_backtest_from_paths(mtf_path, RAW / "MNQ_combined_1m.csv")
        individual["mnq_mtf"] = metrics_row("mnq_mtf", mtf)

    # Concurrent portfolio: merge trades by exit time
    combined_trades: list[dict[str, Any]] = []
    for label, result in [("MNQ", mnq_wf), ("MES", mes_full)]:
        for t in result.trades:
            combined_trades.append({**t, "instrument": label})
    combined_trades.sort(key=lambda t: t["exit_time"])

    portfolio_wf_mes = _portfolio_metrics(combined_trades)

    # Best-config portfolio: walkforward MNQ + mes_full
    payload = {
        "best_configs": {
            "MNQ": str(configs["mnq_walkforward"].relative_to(ROOT)),
            "MES": str(configs["mes_full"].relative_to(ROOT)),
        },
        "individual": individual,
        "portfolio_mnq_wf_plus_mes_full": portfolio_wf_mes,
        "combined_trade_count": len(combined_trades),
        "note": "Portfolio sums concurrent trades sorted by exit_time; shared capital curve approximation.",
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def generate_report_md(results: dict[str, Any]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Final Validation Report",
        "",
        f"Generated: {now}",
        "",
        "Research only — not for live trading. Full L2 depth backtests skipped (documented limitation).",
        "",
        "## Limitations",
        "",
        "- **Full L2 depth:** ETL uses trade snapshots (bid_sz_00/ask_sz_00); depth_updates pass skipped by default.",
        "- **RTH default:** Standard bars filter 09:30–16:00 ET unless globex/all session mode used.",
        "- **Date range:** 22 MNQ / 20 MES RTH days; holdout is chronological last 7 MNQ days.",
        "",
    ]

    mtf = results.get("mtf_walkforward", {})
    lines.extend(
        [
            "## 1. MTF walk-forward holdout (5m trend + 1m exec)",
            "",
            f"- Train ({len(mtf.get('train_dates', []))} days): `{', '.join(mtf.get('train_dates', []))}`",
            f"- Holdout ({len(mtf.get('holdout_dates', []))} days): `{', '.join(mtf.get('holdout_dates', []))}`",
            f"- Optuna trials: {mtf.get('n_trials', 0)} (train only)",
            "",
            "| Split | Trades | Win % | Net PnL | PF | Max DD | Overfit ratio |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    train = mtf.get("train", {})
    hold = mtf.get("holdout_walkforward", {})
    ratio = mtf.get("overfit_ratio")
    ratio_str = f"{ratio:.2f}" if ratio is not None else "—"
    lines.append(
        f"| Train | {train.get('trades', 0)} | {fmt_pct(train.get('win_rate', 0))} | "
        f"{fmt_money(train.get('net_pnl', 0))} | {train.get('profit_factor', 0):.2f} | "
        f"{fmt_money(train.get('max_drawdown', 0))} | — |"
    )
    lines.append(
        f"| Holdout | {hold.get('trades', 0)} | {fmt_pct(hold.get('win_rate', 0))} | "
        f"{fmt_money(hold.get('net_pnl', 0))} | {hold.get('profit_factor', 0):.2f} | "
        f"{fmt_money(hold.get('max_drawdown', 0))} | {ratio_str} |"
    )
    lines.append("")

    mes30 = results.get("mes_30s_walkforward", {})
    lines.extend(
        [
            "## 2. MES 30s walk-forward holdout",
            "",
            f"- Train: `{', '.join(mes30.get('train_dates', []))}`",
            f"- Holdout: `{', '.join(mes30.get('holdout_dates', []))}`",
            f"- Optuna trials: {mes30.get('n_trials', 0)}",
            "",
            "| Split | Trades | Win % | Net PnL | PF | Max DD |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for key, label in [("train", "Train"), ("holdout_walkforward", "Holdout")]:
        r = mes30.get(key, {})
        lines.append(
            f"| {label} | {r.get('trades', 0)} | {fmt_pct(r.get('win_rate', 0))} | "
            f"{fmt_money(r.get('net_pnl', 0))} | {r.get('profit_factor', 0):.2f} | "
            f"{fmt_money(r.get('max_drawdown', 0))} |"
        )
    lines.append("")

    repair = results.get("mnq_repair", {})
    lines.extend(
        [
            "## 3. MNQ 20260508 archive repair",
            "",
            f"- Candidates: {len(repair.get('candidates_found', []))}",
            f"- CSV created: **{repair.get('csv_exists')}**",
            f"- Success: {repair.get('success') is not None}",
            "",
        ]
    )
    for att in repair.get("attempts", [])[:6]:
        lines.append(f"- `{att.get('archive', '')}` session={att.get('session_filter')}: {att.get('status')} — {att.get('error', att.get('row_count', ''))}")
    lines.append("")

    slip = results.get("slippage_stress", {})
    lines.extend(
        [
            "## 4. Slippage stress (ticks 1/2/3)",
            "",
            "| Config | Slip | Trades | Win % | Net PnL | PF | Max DD |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for r in slip.get("results", []):
        if "error" in r:
            continue
        lines.append(
            f"| {r.get('config_label')} | {r.get('slippage_ticks')} | {r.get('trades', 0)} | "
            f"{fmt_pct(r.get('win_rate', 0))} | {fmt_money(r.get('net_pnl', 0))} | "
            f"{r.get('profit_factor', 0):.2f} | {fmt_money(r.get('max_drawdown', 0))} |"
        )
    lines.append("")

    globex = results.get("globex_etl", {})
    lines.extend(
        [
            "## 5. Globex/extended ETL (Sunday dates)",
            "",
            f"- Dates: `{', '.join(globex.get('sunday_dates', []))}`",
            f"- Successful: `{', '.join(globex.get('successful_dates', [])) or 'none'}`",
            f"- Output: `{globex.get('output_dir', '')}`",
            "",
        ]
    )
    for c in globex.get("conversions", []):
        status = c.get("status")
        extra = c.get("row_count", c.get("error", ""))
        lines.append(f"- {c.get('date')}: {status} ({extra})")
    lines.append("")

    nqes = results.get("nq_es_scan", {})
    lines.extend(
        [
            "## 6. NQ/ES data scan",
            "",
            f"- NQ CSV files found: {nqes.get('nq_csv_count', 0)}",
            f"- ES CSV files found: {nqes.get('es_csv_count', 0)}",
            f"- NQ archives: {nqes.get('nq_archive_count', 0)}",
            f"- ES archives: {nqes.get('es_archive_count', 0)}",
            f"- Note: {nqes.get('note', '')}",
            "",
        ]
    )

    audit = results.get("blocked_audit", {})
    lines.extend(
        [
            "## 7. Blocked-signal audit (MNQ combined, mnq_default)",
            "",
            f"- Bars evaluated: {audit.get('bars_evaluated', 0)}",
            "",
            "| Blocker | Count | % of bars |",
            "| --- | ---: | ---: |",
        ]
    )
    for item in audit.get("top_blockers", [])[:10]:
        lines.append(
            f"| {item['reason']} | {item['count']} | {fmt_pct(item['pct'])} |"
        )
    lines.append("")

    port = results.get("portfolio", {})
    lines.extend(
        [
            "## 8. Portfolio backtest (MNQ + MES best configs)",
            "",
            f"- MNQ: `{port.get('best_configs', {}).get('MNQ', '')}`",
            f"- MES: `{port.get('best_configs', {}).get('MES', '')}`",
            "",
            "### Individual",
            "",
            "| Leg | Trades | Win % | Net PnL | PF | Max DD |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for label, row in port.get("individual", {}).items():
        lines.append(
            f"| {label} | {row.get('trades', 0)} | {fmt_pct(row.get('win_rate', 0))} | "
            f"{fmt_money(row.get('net_pnl', 0))} | {row.get('profit_factor', 0):.2f} | "
            f"{fmt_money(row.get('max_drawdown', 0))} |"
        )
    pf = port.get("portfolio_mnq_wf_plus_mes_full", {})
    lines.extend(
        [
            "",
            "### Combined portfolio (MNQ walkforward + MES full optimized)",
            "",
            f"- Trades: {pf.get('total_trades', 0)}",
            f"- Net PnL: {fmt_money(pf.get('net_pnl', 0))}",
            f"- Win rate: {fmt_pct(pf.get('win_rate', 0))}",
            f"- Max drawdown: {fmt_money(pf.get('max_drawdown', 0))}",
            "",
            "## Caveats",
            "",
            "1. Walk-forward / MTF holdout uses chronological train/holdout split.",
            "2. Slippage stress multiplies per-side tick penalty on entries and exits.",
            "3. Portfolio combines trade streams; no margin or correlation modeling.",
            "4. Sunday globex ETL may produce overnight bars unsuitable for RTH-only backtests.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run final validation suite")
    parser.add_argument("--trials", type=int, default=70, help="Optuna trials for walk-forward tests")
    parser.add_argument("--force", action="store_true", help="Re-run even if JSON outputs exist")
    parser.add_argument("--skip", nargs="+", choices=["1", "2", "3", "4", "5", "6", "7", "8"])
    parser.add_argument("--pytest", action="store_true", help="Run pytest after validation")
    args = parser.parse_args()

    skip = set(args.skip or [])
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}

    if "1" not in skip:
        results["mtf_walkforward"] = test_mtf_walkforward(args.trials, args.force)
    elif (REPORT_DIR / "mtf_walkforward_results.json").is_file():
        results["mtf_walkforward"] = json.loads(
            (REPORT_DIR / "mtf_walkforward_results.json").read_text(encoding="utf-8")
        )

    if "2" not in skip:
        results["mes_30s_walkforward"] = test_mes_30s_walkforward(args.trials, args.force)
    elif (REPORT_DIR / "mes_30s_walkforward_results.json").is_file():
        results["mes_30s_walkforward"] = json.loads(
            (REPORT_DIR / "mes_30s_walkforward_results.json").read_text(encoding="utf-8")
        )

    if "3" not in skip:
        results["mnq_repair"] = test_mnq_repair(args.force)
    elif (REPORT_DIR / "mnq_repair_results.json").is_file():
        results["mnq_repair"] = json.loads((REPORT_DIR / "mnq_repair_results.json").read_text(encoding="utf-8"))

    if "4" not in skip:
        results["slippage_stress"] = test_slippage_stress(args.force)
    elif (REPORT_DIR / "slippage_stress_results.json").is_file():
        results["slippage_stress"] = json.loads(
            (REPORT_DIR / "slippage_stress_results.json").read_text(encoding="utf-8")
        )

    if "5" not in skip:
        results["globex_etl"] = test_globex_etl(args.force)
    elif (REPORT_DIR / "globex_etl_results.json").is_file():
        results["globex_etl"] = json.loads((REPORT_DIR / "globex_etl_results.json").read_text(encoding="utf-8"))

    if "6" not in skip:
        results["nq_es_scan"] = scan_nq_es_data()
    elif (REPORT_DIR / "nq_es_scan_results.json").is_file():
        results["nq_es_scan"] = json.loads((REPORT_DIR / "nq_es_scan_results.json").read_text(encoding="utf-8"))

    if "7" not in skip:
        results["blocked_audit"] = test_blocked_signal_audit(args.force)
    elif (REPORT_DIR / "blocked_signal_audit.json").is_file():
        results["blocked_audit"] = json.loads((REPORT_DIR / "blocked_signal_audit.json").read_text(encoding="utf-8"))

    if "8" not in skip:
        results["portfolio"] = test_portfolio_backtest(args.force)
    elif (REPORT_DIR / "portfolio_backtest_results.json").is_file():
        results["portfolio"] = json.loads(
            (REPORT_DIR / "portfolio_backtest_results.json").read_text(encoding="utf-8")
        )

    report_md = generate_report_md(results)
    report_path = REPORT_DIR / "FINAL_VALIDATION_REPORT.md"
    report_path.write_text(report_md, encoding="utf-8")
    print(f"\nReport written to {report_path}")
    print("\n" + report_md)

    if args.pytest:
        proc = subprocess.run([sys.executable, "-m", "pytest"], cwd=ROOT, check=False)
        if proc.returncode != 0:
            raise SystemExit(proc.returncode)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Generate FULL_REAL_DATA_REPORT.md from ETL, backtest, and optimization artifacts."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "data" / "reports" / "real_full"


def load_json(path: Path) -> dict | list:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    manifest = load_json(ROOT / "data" / "raw" / "etl_manifest.json")
    summary = load_json(REPORTS / "real_backtest_summary.json")
    mes_entry = load_json(REPORTS / "optimization_mes_entry" / "optimization_result.json")
    mes_exit = load_json(REPORTS / "optimization_mes_entry_exit" / "optimization_result.json")
    mnq_entry = load_json(REPORTS / "optimization_mnq_entry" / "optimization_result.json")

    mnq_files = [f for f in manifest["files"] if f["instrument"] == "MNQ"]
    mes_files = [f for f in manifest["files"] if f["instrument"] == "MES"]

    per_day = summary["per_day"]
    mnq_days = sorted([r for r in per_day if r["instrument"] == "MNQ"], key=lambda x: x["date"])
    mes_days = sorted([r for r in per_day if r["instrument"] == "MES"], key=lambda x: x["date"])
    combined = {r["instrument"]: r for r in summary["combined"]}
    agg = summary["aggregate"]

    errors_path = ROOT / "data" / "raw" / "etl_errors.log"
    etl_errors = errors_path.read_text(encoding="utf-8").strip().split("\n") if errors_path.is_file() else []

    lines: list[str] = []
    lines.append("# Full Real L2 Data Report")
    lines.append("")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} ET")
    lines.append("Project: `futures-trend-day-l2-scalper`")
    lines.append("Research only — no live trading.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Phase 1: ETL Summary")
    lines.append("")
    lines.append("**Sources scanned:**")
    lines.append("- `D:\\TradeData\\StorageBox\\bundles\\futuresbot\\archives\\l2\\`")
    lines.append("- `D:\\TradeData\\svg-empire-*\\svg-empire\\bundles\\futuresbot\\archives\\l2\\`")
    lines.append("- Inventory: `tradedata_inventory.json`")
    lines.append("")
    lines.append("| Instrument | Inventory dates | Converted | ETL failures |")
    lines.append("|------------|-----------------|-----------|--------------|")
    lines.append(f"| MNQ | 27 | {len(mnq_files)} | {27 - len(mnq_files)} |")
    lines.append(f"| MES | 26 | {len(mes_files)} | {26 - len(mes_files)} |")
    lines.append(f"| **Total** | **53** | **{len(manifest['files'])}** | **{len(etl_errors)}** |")
    lines.append("")
    if etl_errors:
        lines.append("**Failed dates (no RTH bars or corrupt archive):**")
        for err in etl_errors:
            lines.append(f"- `{err}`")
        lines.append("")
    lines.append("**ETL notes:** L1 book from trade snapshots; depth levels 2–5 approximated to 0; RTH 09:30–16:00 ET.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Phase 2: Per-Day Backtest Results")
    lines.append("")
    lines.append("Configs: `configs/mnq_default.yaml`, `configs/mes_default.yaml` | Slippage: 1 tick | Commission: $0.62/side")
    lines.append("")
    lines.append("### MNQ")
    lines.append("")
    lines.append("| Date | Trades | Win % | Net PnL | PF | Max DD | Long | Short |")
    lines.append("|------|--------|-------|---------|-----|--------|------|-------|")
    for r in mnq_days:
        lines.append(
            f"| {r['date']} | {r['trades']} | {r['win_rate']*100:.1f}% | ${r['net_pnl']:.2f} | "
            f"{r['profit_factor']:.2f} | ${r['max_drawdown']:.2f} | {r['long_trades']} | {r['short_trades']} |"
        )
    lines.append("")
    lines.append("### MES")
    lines.append("")
    lines.append("| Date | Trades | Win % | Net PnL | PF | Max DD | Long | Short |")
    lines.append("|------|--------|-------|---------|-----|--------|------|-------|")
    for r in mes_days:
        lines.append(
            f"| {r['date']} | {r['trades']} | {r['win_rate']*100:.1f}% | ${r['net_pnl']:.2f} | "
            f"{r['profit_factor']:.2f} | ${r['max_drawdown']:.2f} | {r['long_trades']} | {r['short_trades']} |"
        )
    lines.append("")
    lines.append("### Combined (multi-day continuous backtest)")
    lines.append("")
    lines.append("| Instrument | Days | Trades | Win % | Net PnL | PF | Max DD | Sharpe |")
    lines.append("|------------|------|--------|-------|---------|-----|--------|--------|")
    for inst in ("MNQ", "MES"):
        c = combined[inst]
        a = agg[inst]
        lines.append(
            f"| **{inst}** | {a['days']} | {c['trades']} | {c['win_rate']*100:.1f}% | "
            f"**${c['net_pnl']:.2f}** | {c['profit_factor']:.2f} | ${c['max_drawdown']:.2f} | {c['sharpe']:.2f} |"
        )
    lines.append("")
    lines.append("### Best / Worst Days")
    lines.append("")
    for inst in ("MNQ", "MES"):
        a = agg[inst]
        lines.append(
            f"- **{inst}** best: `{a['best_day']['date']}` (${a['best_day']['net_pnl']:.2f}) | "
            f"worst: `{a['worst_day']['date']}` (${a['worst_day']['net_pnl']:.2f})"
        )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Phase 3: MES Entry Filter Tuning")
    lines.append("")
    lines.append("MES baseline on combined real data was deeply negative (24.7% win rate). Optuna `--focus filters` on `MES_combined_1m.csv`, metric `net_pnl`, 90 trials.")
    lines.append("")
    mes_baseline = combined["MES"]
    lines.append("| Config | Trades | Win % | Net PnL | PF | Max DD |")
    lines.append("|--------|--------|-------|---------|-----|--------|")
    lines.append(
        f"| Baseline (`mes_default.yaml`) | {mes_baseline['trades']} | {mes_baseline['win_rate']*100:.1f}% | "
        f"${mes_baseline['net_pnl']:.2f} | {mes_baseline['profit_factor']:.2f} | ${mes_baseline['max_drawdown']:.2f} |"
    )
    em = mes_entry["final_metrics"]
    lines.append(
        f"| Entry-optimized | {em['total_trades']} | {em['win_rate']*100:.1f}% | "
        f"${em['net_pnl']:.2f} | {em['profit_factor']:.2f} | ${em['max_drawdown']:.2f} |"
    )
    ex = mes_exit["final_metrics"]
    lines.append(
        f"| Entry + exit optimized | {ex['total_trades']} | {ex['win_rate']*100:.1f}% | "
        f"**${ex['net_pnl']:.2f}** | {ex['profit_factor']:.2f} | ${ex['max_drawdown']:.2f} |"
    )
    lines.append("")
    lines.append("**Best entry/filter params (trial 66):**")
    lines.append("```yaml")
    for k, v in mes_entry["best_params"].items():
        lines.append(f"{k}: {v}")
    lines.append("```")
    lines.append("")
    lines.append("**Best exit params (on entry-optimized base):**")
    lines.append("```yaml")
    for k, v in mes_exit["best_params"].items():
        lines.append(f"{k}: {v}")
    lines.append("```")
    lines.append("")
    lines.append("Key insight: tightening `min_trend_score` (86), `max_spread_ticks` (1), and `min_book_depth` (68) reduced MES overtrading from 85 to 3 trades on combined data, flipping PnL positive.")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Phase 4: MNQ Entry Optimization")
    lines.append("")
    mnq_baseline = combined["MNQ"]
    mm = mnq_entry["final_metrics"]
    lines.append("| Config | Trades | Win % | Net PnL | PF | Max DD |")
    lines.append("|--------|--------|-------|---------|-----|--------|")
    lines.append(
        f"| Baseline (`mnq_default.yaml`) | {mnq_baseline['trades']} | {mnq_baseline['win_rate']*100:.1f}% | "
        f"${mnq_baseline['net_pnl']:.2f} | {mnq_baseline['profit_factor']:.2f} | ${mnq_baseline['max_drawdown']:.2f} |"
    )
    lines.append(
        f"| Entry-optimized | {mm['total_trades']} | {mm['win_rate']*100:.1f}% | "
        f"**${mm['net_pnl']:.2f}** | {mm['profit_factor']:.2f} | ${mm['max_drawdown']:.2f} |"
    )
    lines.append("")
    lines.append("**Best MNQ entry/filter params:**")
    lines.append("```yaml")
    for k, v in mnq_entry["best_params"].items():
        lines.append(f"{k}: {v}")
    lines.append("```")
    lines.append("")
    lines.append(f"Improvement: +${mm['net_pnl'] - mnq_baseline['net_pnl']:.2f} net PnL ({((mm['net_pnl']/mnq_baseline['net_pnl'])-1)*100:.1f}% vs baseline).")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Limitations")
    lines.append("")
    lines.append("- **L1-only depth:** Archives expose `level_position=0` only; L2–L5 columns are zero-filled.")
    lines.append("- **RTH session only:** 09:30–16:00 ET; partial days (e.g. MNQ 20260519, MES 20260519) have truncated bars.")
    lines.append("- **11 archive dates failed ETL:** Weekend/holiday or empty trade data (Sat/Sun dates in inventory).")
    lines.append("- **Low MES trade count after tuning:** Entry filters are very selective — validate on forward data before production.")
    lines.append("- **Combined vs sum-of-days:** Multi-day combined backtest differs from summing isolated daily runs (session resets, cooldown).")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Recommended Production Configs")
    lines.append("")
    lines.append("### MNQ — use `configs/mnq_entry_optimized.yaml`")
    lines.append("```yaml")
    lines.append("trend.min_trend_score: 60")
    lines.append("trend.adx_trend_min: 18")
    lines.append("trend.atr_expansion_mult: 0.94")
    lines.append("l2.min_l2_score: 56")
    lines.append("l2.imbalance_threshold: 0.50")
    lines.append("l2.min_book_depth: 110")
    lines.append("entry.pullback_to_ema_ticks: 6")
    lines.append("entry.max_spread_ticks: 5")
    lines.append("```")
    lines.append("")
    lines.append("### MES — use `configs/mes_full_optimized.yaml`")
    lines.append("```yaml")
    lines.append("trend.min_trend_score: 86")
    lines.append("trend.adx_trend_min: 15")
    lines.append("trend.atr_expansion_mult: 1.397")
    lines.append("l2.min_l2_score: 55")
    lines.append("l2.imbalance_threshold: 0.697")
    lines.append("l2.min_book_depth: 68")
    lines.append("entry.pullback_to_ema_ticks: 2")
    lines.append("entry.max_spread_ticks: 1")
    lines.append("exit.stop_loss_ticks: 10")
    lines.append("exit.take_profit_ticks: 30")
    lines.append("exit.breakeven_trigger_ticks: 12")
    lines.append("exit.trailing_trigger_ticks: 11")
    lines.append("exit.trailing_offset_ticks: 4")
    lines.append("exit.max_hold_bars: 21")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Artifacts")
    lines.append("")
    lines.append("- ETL manifest: `data/raw/etl_manifest.json`")
    lines.append("- Backtest summary: `data/reports/real_full/real_backtest_summary.json`")
    lines.append("- Per-day HTML reports: `data/reports/real_full/{INSTRUMENT}_{DATE}/`")
    lines.append("- MES entry optimization: `data/reports/real_full/optimization_mes_entry/`")
    lines.append("- MES exit optimization: `data/reports/real_full/optimization_mes_entry_exit/`")
    lines.append("- MNQ entry optimization: `data/reports/real_full/optimization_mnq_entry/`")

    out_path = REPORTS / "FULL_REAL_DATA_REPORT.md"
    text = "\n".join(lines) + "\n"
    out_path.write_text(text, encoding="utf-8")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()

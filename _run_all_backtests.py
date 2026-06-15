"""Run all backtests and write BACKTEST_SUMMARY.md"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[0] if "__file__" in dir() else Path.cwd()
ROOT = Path(r"D:\AI_Vault\workspace\jarvis-one\futures-trend-day-l2-scalper")
sys.path.insert(0, str(ROOT))

from scalper.backtest import run_backtest_from_paths
from scalper.reports import generate_report

SAMPLE_DIR = ROOT / "data" / "sample"
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "reports" / "all_runs"
DAY_PATTERN = re.compile(r"^(MNQ|MES)_(\d{8})_1m\.csv$", re.IGNORECASE)

SAMPLE_RUNS = [
    ("mnq_trend_up.csv", "configs/mnq_default.yaml"),
    ("mnq_trend_down.csv", "configs/mnq_default.yaml"),
    ("mnq_chop.csv", "configs/mnq_default.yaml"),
    ("mes_trend_up.csv", "configs/mes_default.yaml"),
    ("mes_chop.csv", "configs/mes_default.yaml"),
]

COMBINED_RUNS = [
    ("MNQ_combined_1m.csv", "configs/mnq_default.yaml", "MNQ_combined_baseline"),
    ("MNQ_combined_1m.csv", "configs/mnq_entry_optimized.yaml", "MNQ_combined_mnq_entry_optimized"),
    ("MES_combined_1m.csv", "configs/mes_default.yaml", "MES_combined_baseline"),
    ("MES_combined_1m.csv", "configs/mes_full_optimized.yaml", "MES_combined_mes_full_optimized"),
]


def run_one(cfg_rel: str, data_path: Path, out_rel: str) -> dict:
    cfg = ROOT / cfg_rel
    out_dir = ROOT / out_rel
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = run_backtest_from_paths(cfg, data_path)
        generate_report(result, out_dir)
        m = result.metrics
        long_c = sum(1 for t in result.trades if str(t.get("side", "")).lower() in ("long", "side.long"))
        short_c = sum(1 for t in result.trades if str(t.get("side", "")).lower() in ("short", "side.short"))
        if long_c + short_c == 0:
            for t in result.trades:
                s = str(t.get("side", "")).upper()
                if "LONG" in s:
                    long_c += 1
                elif "SHORT" in s:
                    short_c += 1
        row = {
            "name": out_dir.name,
            "data": data_path.name,
            "config": cfg.name,
            "trades": m.total_trades,
            "win_rate": m.win_rate,
            "net_pnl": m.net_pnl,
            "profit_factor": m.profit_factor,
            "max_drawdown": m.max_drawdown,
            "long_trades": long_c,
            "short_trades": short_c,
            "l2_approximated": result.l2_approximated,
            "error": None,
        }
        summary_path = out_dir / "summary.json"
        summary_path.write_text(
            json.dumps({"metrics": m.model_dump(), "warnings": result.warnings, "l2_approximated": result.l2_approximated, "long_trades": long_c, "short_trades": short_c}, indent=2),
            encoding="utf-8",
        )
        print(f"OK {out_dir.name}: trades={m.total_trades} pnl={m.net_pnl:.2f}")
        return row
    except Exception as e:
        print(f"FAIL {out_dir.name}: {e}")
        return {
            "name": out_dir.name,
            "data": data_path.name,
            "config": cfg.name,
            "trades": 0,
            "win_rate": 0.0,
            "net_pnl": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "long_trades": 0,
            "short_trades": 0,
            "l2_approximated": False,
            "error": str(e),
        }


def fmt_pct(x: float) -> str:
    return f"{x*100:.1f}%" if x <= 1 else f"{x:.1f}%"

def fmt_money(x: float) -> str:
    return f"${x:,.2f}"

def table(rows: list[dict], cols: list[tuple[str, str]]) -> str:
    if not rows:
        return "_No runs._\n"
    hdr = "| " + " | ".join(c[0] for c in cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    lines = [hdr, sep]
    for r in rows:
        cells = []
        for _, key in cols:
            v = r.get(key, "")
            if key == "win_rate" and isinstance(v, (int, float)):
                cells.append(fmt_pct(v))
            elif key in ("net_pnl", "max_drawdown") and isinstance(v, (int, float)):
                cells.append(fmt_money(v))
            elif key == "profit_factor" and isinstance(v, (int, float)):
                cells.append(f"{v:.2f}")
            elif key == "l2_approximated":
                cells.append("yes" if v else "no")
            elif key == "error" and v:
                cells.append(str(v)[:40])
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"

sample_rows = []
for fname, cfg in SAMPLE_RUNS:
    base = Path(fname).stem
    sample_rows.append(run_one(cfg, SAMPLE_DIR / fname, f"data/reports/all_runs/sample/{base}"))

real_mnq = []
real_mes = []
for csv_path in sorted(RAW.glob("*_1m.csv")):
    if not DAY_PATTERN.match(csv_path.name):
        continue
    inst = csv_path.name[:3].upper()
    cfg = "configs/mes_default.yaml" if inst == "MES" else "configs/mnq_default.yaml"
    base = csv_path.stem
    row = run_one(cfg, csv_path, f"data/reports/all_runs/real/{base}")
    row["date"] = DAY_PATTERN.match(csv_path.name).group(2)
    if inst == "MNQ":
        real_mnq.append(row)
    else:
        real_mes.append(row)

combined_rows = []
for fname, cfg, out_name in COMBINED_RUNS:
    combined_rows.append(run_one(cfg, RAW / fname, f"data/reports/all_runs/combined/{out_name}"))

def agg(rows: list[dict]) -> dict:
    ok = [r for r in rows if not r.get("error")]
    if not ok:
        return {}
    tt = sum(r["trades"] for r in ok)
    wins = sum(r["trades"] * r["win_rate"] for r in ok)
    net = sum(r["net_pnl"] for r in ok)
    gp = sum(max(r["net_pnl"], 0) for r in ok)  # rough
    return {
        "runs": len(ok),
        "trades": tt,
        "win_rate": wins / tt if tt else 0,
        "net_pnl": net,
        "max_dd": max(r["max_drawdown"] for r in ok),
    }

all_real = real_mnq + real_mes
grand = {
    "sample": agg(sample_rows),
    "real_mnq": agg(real_mnq),
    "real_mes": agg(real_mes),
    "real_all": agg(all_real),
    "combined": agg(combined_rows),
}

COLS_SAMPLE = [
    ("Run", "name"), ("Config", "config"), ("Trades", "trades"), ("Win %", "win_rate"),
    ("Net PnL", "net_pnl"), ("PF", "profit_factor"), ("Max DD", "max_drawdown"),
    ("Long", "long_trades"), ("Short", "short_trades"), ("L2 approx", "l2_approximated"),
]
COLS_REAL = [
    ("Date", "date"), ("File", "data"), ("Trades", "trades"), ("Win %", "win_rate"),
    ("Net PnL", "net_pnl"), ("PF", "profit_factor"), ("Max DD", "max_drawdown"),
    ("Long", "long_trades"), ("Short", "short_trades"), ("L2 approx", "l2_approximated"),
]
COLS_COMB = [
    ("Run", "name"), ("Config", "config"), ("Trades", "trades"), ("Win %", "win_rate"),
    ("Net PnL", "net_pnl"), ("PF", "profit_factor"), ("Max DD", "max_drawdown"),
    ("Long", "long_trades"), ("Short", "short_trades"), ("L2 approx", "l2_approximated"),
]

md = f"""# Backtest Summary (all runs)

Generated from `data/reports/all_runs/`.

## Headline numbers

| Scope | Runs | Trades | Win rate | Net PnL | Max DD (worst run) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Sample data | {grand['sample'].get('runs', 0)} | {grand['sample'].get('trades', 0)} | {fmt_pct(grand['sample'].get('win_rate', 0))} | {fmt_money(grand['sample'].get('net_pnl', 0))} | {fmt_money(grand['sample'].get('max_dd', 0))} |
| Real MNQ (per-day) | {grand['real_mnq'].get('runs', 0)} | {grand['real_mnq'].get('trades', 0)} | {fmt_pct(grand['real_mnq'].get('win_rate', 0))} | {fmt_money(grand['real_mnq'].get('net_pnl', 0))} | {fmt_money(grand['real_mnq'].get('max_dd', 0))} |
| Real MES (per-day) | {grand['real_mes'].get('runs', 0)} | {grand['real_mes'].get('trades', 0)} | {fmt_pct(grand['real_mes'].get('win_rate', 0))} | {fmt_money(grand['real_mes'].get('net_pnl', 0))} | {fmt_money(grand['real_mes'].get('max_dd', 0))} |
| All real per-day | {grand['real_all'].get('runs', 0)} | {grand['real_all'].get('trades', 0)} | {fmt_pct(grand['real_all'].get('win_rate', 0))} | {fmt_money(grand['real_all'].get('net_pnl', 0))} | {fmt_money(grand['real_all'].get('max_dd', 0))} |
| Combined runs | {grand['combined'].get('runs', 0)} | {grand['combined'].get('trades', 0)} | {fmt_pct(grand['combined'].get('win_rate', 0))} | {fmt_money(grand['combined'].get('net_pnl', 0))} | {fmt_money(grand['combined'].get('max_dd', 0))} |

### Sample data results

{table(sample_rows, COLS_SAMPLE)}

### Real data per-day MNQ

{table(real_mnq, COLS_REAL)}

### Real data per-day MES

{table(real_mes, COLS_REAL)}

### Combined runs (baseline vs optimized)

{table(combined_rows, COLS_COMB)}

### Grand totals

- **Sample net PnL (sum of 5 runs):** {fmt_money(grand['sample'].get('net_pnl', 0))}
- **Real MNQ net PnL (sum of days):** {fmt_money(grand['real_mnq'].get('net_pnl', 0))}
- **Real MES net PnL (sum of days):** {fmt_money(grand['real_mes'].get('net_pnl', 0))}
- **All real per-day net PnL:** {fmt_money(grand['real_all'].get('net_pnl', 0))}
- **Combined runs net PnL (4 runs, not additive with per-day):** see combined table above

"""

summary_path = OUT / "BACKTEST_SUMMARY.md"
summary_path.write_text(md, encoding="utf-8")
(OUT / "all_runs_data.json").write_text(json.dumps({
    "sample": sample_rows, "real_mnq": real_mnq, "real_mes": real_mes, "combined": combined_rows, "grand": grand
}, indent=2), encoding="utf-8")
print(f"Wrote {summary_path}")

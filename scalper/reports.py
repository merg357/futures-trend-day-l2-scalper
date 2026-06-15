"""Report generation: JSON, CSV, HTML, PNG charts."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from jinja2 import Template

from scalper.models import BacktestResult


HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{{ symbol }} Backtest Report</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 2rem; background: #0f1419; color: #e7e9ea; }
    h1, h2 { color: #1d9bf0; }
    table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
    th, td { border: 1px solid #38444d; padding: 0.5rem; text-align: left; }
    th { background: #1a2332; }
    .warn { color: #ffad1f; background: #2a2210; padding: 0.75rem; border-radius: 6px; }
    .metric-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; }
    .card { background: #1a2332; padding: 1rem; border-radius: 8px; }
    img { max-width: 100%; margin: 1rem 0; }
  </style>
</head>
<body>
  <h1>{{ symbol }} — Trend-Day L2 Pullback Scalper</h1>
  <p><strong>Data:</strong> {{ data_path }}</p>
  <p><strong>Period:</strong> {{ start_time }} → {{ end_time }}</p>
  {% if l2_approximated %}
  <div class="warn">⚠ L2 approximation mode was used (book columns missing).</div>
  {% endif %}
  {% for w in warnings %}
  <div class="warn">{{ w }}</div>
  {% endfor %}
  <h2>Metrics</h2>
  <div class="metric-grid">
    {% for k, v in metrics.items() %}
    <div class="card"><strong>{{ k }}</strong><br>{{ v }}</div>
    {% endfor %}
  </div>
  <h2>Equity Curve</h2>
  <img src="equity_curve.png" alt="Equity curve">
  <h2>Trade Distribution</h2>
  <img src="pnl_distribution.png" alt="PnL distribution">
  <h2>Trades (last 20)</h2>
  <table>
    <tr>{% for col in trade_columns %}<th>{{ col }}</th>{% endfor %}</tr>
    {% for row in trades_tail %}
    <tr>{% for col in trade_columns %}<td>{{ row[col] }}</td>{% endfor %}</tr>
    {% endfor %}
  </table>
</body>
</html>
"""


def save_json(result: BacktestResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "report.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(result.model_dump(), fh, indent=2, default=str)
    return path


def save_csv(result: BacktestResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "trades.csv"
    if result.trades:
        pd.DataFrame(result.trades).to_csv(path, index=False)
    else:
        pd.DataFrame(columns=["side", "pnl", "exit_reason"]).to_csv(path, index=False)
    metrics_path = out_dir / "metrics.csv"
    pd.DataFrame([result.metrics.model_dump()]).to_csv(metrics_path, index=False)
    return path


def save_charts(result: BacktestResult, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(result.equity_curve, color="#1d9bf0", linewidth=1.5)
    ax.set_title(f"{result.symbol} Equity Curve")
    ax.set_xlabel("Bar")
    ax.set_ylabel("Equity ($)")
    ax.grid(True, alpha=0.3)
    eq_path = out_dir / "equity_curve.png"
    fig.savefig(eq_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    paths.append(eq_path)

    if result.trades:
        pnls = [t["pnl"] for t in result.trades]
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.bar(range(len(pnls)), pnls, color=["#00ba7c" if p > 0 else "#f4212e" for p in pnls])
        ax.set_title("Trade PnL")
        ax.set_xlabel("Trade #")
        ax.set_ylabel("PnL ($)")
        ax.axhline(0, color="white", linewidth=0.5)
        pnl_path = out_dir / "pnl_distribution.png"
        fig.savefig(pnl_path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        paths.append(pnl_path)
    else:
        fig, ax = plt.subplots(figsize=(6, 3))
        ax.text(0.5, 0.5, "No trades", ha="center", va="center")
        pnl_path = out_dir / "pnl_distribution.png"
        fig.savefig(pnl_path, dpi=120)
        plt.close(fig)
        paths.append(pnl_path)

    return paths


def save_html(result: BacktestResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    trades_tail = result.trades[-20:] if result.trades else []
    trade_columns = list(result.trades[0].keys()) if result.trades else []
    tmpl = Template(HTML_TEMPLATE)
    html = tmpl.render(
        symbol=result.symbol,
        data_path=result.data_path,
        start_time=result.start_time,
        end_time=result.end_time,
        metrics=result.metrics.model_dump(),
        warnings=result.warnings,
        l2_approximated=result.l2_approximated,
        trades_tail=trades_tail,
        trade_columns=trade_columns,
    )
    path = out_dir / "report.html"
    path.write_text(html, encoding="utf-8")
    return path


def generate_report(result: BacktestResult, out_dir: str | Path) -> dict[str, Path]:
    out = Path(out_dir)
    return {
        "json": save_json(result, out),
        "csv": save_csv(result, out),
        "html": save_html(result, out),
        "charts": save_charts(result, out),
    }

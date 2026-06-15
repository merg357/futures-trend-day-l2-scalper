# Futures Trend-Day L2 Pullback Scalper

**Research and backtesting only.** No live trading. No real orders.

Event-driven backtester for MNQ/MES/NQ/ES trend-day pullback scalping with L2 order-book scoring (or OHLCV approximation when L2 columns are absent).

## Strategy Overview

1. **Trend score (0–100)** — EMA alignment, VWAP, ADX, ATR expansion, structure → `LONG` / `SHORT` / `NONE` bias
2. **L2 score (0–100)** — bid/ask imbalance, depth, delta, absorption
3. **Entry** — chop filter (low ADX / tight range), pullback to fast EMA, L2 confirmation
4. **Exit** — stop, target, breakeven, trailing, max hold time, L2 reversal, session flatten
5. **Risk** — max trades/day, daily loss cap, consecutive loss halt, position sizing

## Quick Start

```bash
pip install -r requirements.txt
python scripts/generate_sample_data.py
python scripts/run_backtest.py --config configs/mnq_default.yaml --data data/sample/mnq_trend_up.csv --out data/reports/mnq_trend_up
python scripts/run_optimization.py --config configs/mnq_default.yaml --data data/sample/mnq_trend_up.csv --trials 25
pytest
```

## Project Layout

```
futures-trend-day-l2-scalper/
├── configs/          # MNQ & MES default YAML params
├── data/             # sample, raw, processed, reports
├── scalper/          # core library
├── scripts/          # CLI entry points
└── tests/            # pytest suite
```

## Data Format

CSV columns (required): `timestamp`, `open`, `high`, `low`, `close`, `volume`

Optional L2 columns: `bid_size`, `ask_size`, `bid_depth`, `ask_depth`, `delta`

When L2 columns are missing, the backtester enables **approximation mode** (derived from OHLCV) and records a warning in reports.

## Reports

Each backtest writes to the `--out` directory:

- `report.json` — full results
- `trades.csv` / `metrics.csv`
- `report.html`
- `equity_curve.png`, `pnl_distribution.png`

## Disclaimer

This software is for educational research only. Past performance does not guarantee future results. Do not use for live trading without independent validation and proper risk controls.

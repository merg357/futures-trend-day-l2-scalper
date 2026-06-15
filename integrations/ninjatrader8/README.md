# NinjaTrader 8 — ScalperL2Exporter

Exports **1-minute OHLCV + Level II** from the **NinjaTrader 8 data stream** (Rithmic, sim, or other connected feed) to a CSV file consumed by `scalper/paper_runner.py --mode follow`.

This replaces the deprecated **futuresbot L2 recorder** path for live paper trading. Historical tarball ETL still uses `FUTURESBOT_ARCHIVE_ROOT` offline only.

## Architecture

```
NT8 (Rithmic / sim feed)
  → OnMarketDepth (L1–L5) + OnMarketData (last trades)
  → ScalperL2Exporter strategy (1m chart, OnBarClose)
  → append CSV (default: C:\Bots\futures-trend-day-l2-scalper\data\live\nt8_mnq_1m.csv)
  → Python paper_runner.py --mode follow (polls BAR_CSV_PATH)
```

## Install

1. **Copy strategy file** into NinjaTrader custom strategies:

   ```
   %USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\Strategies\ScalperL2Exporter.cs
   ```

   Or from repo:

   ```
   integrations\ninjatrader8\ScalperL2Exporter.cs
   ```

2. Open **NinjaTrader 8** → **New** → **NinjaScript Editor**.

3. Press **F5** (Compile). Fix any compile errors (requires NT8 8.x with Strategies namespace).

4. Confirm **ScalperL2Exporter** appears under **Strategies** in the NinjaScript Explorer.

## Chart setup

1. **Connections** → connect **Rithmic** (or sim) with market data enabled for **MNQ**.
2. Open an **MNQ** chart.
3. Set interval to **1 Minute**.
4. Right-click chart → **Strategies** → add **ScalperL2Exporter**.
5. Set **ExportPath** to match your Python `.env`:

   | Variable | Default |
   |----------|---------|
   | `BAR_CSV_PATH` | `C:\Bots\futures-trend-day-l2-scalper\data\live\nt8_mnq_1m.csv` |
   | `NT8_EXPORT_PATH` | Same path (alias) |

6. Enable the strategy (**Enabled** checkbox). Use **Calculate on bar close** (strategy default).

7. Verify CSV grows during session:

   ```powershell
   Get-Content C:\Bots\futures-trend-day-l2-scalper\data\live\nt8_mnq_1m.csv -Tail 3
   ```

## CSV format

Header (written once):

```
timestamp,open,high,low,close,volume,bid,ask,bid_size,ask_size,bid_depth,ask_depth,delta
```

| Column | Source |
|--------|--------|
| OHLCV | Primary 1m bar series |
| `bid`, `ask` | Level 1 from `OnMarketDepth` |
| `bid_size`, `ask_size` | Level 1 size |
| `bid_depth`, `ask_depth` | Sum of sizes at depth levels 1–5 |
| `delta` | Signed last-trade volume vs top of book within bar |

Timestamps use the chart/session time from NT8 (align with your backtest timezone, typically US/Eastern RTH).

## Start Python paper runner

After NT8 is connected and the strategy is enabled:

```powershell
Set-Location C:\Bots\futures-trend-day-l2-scalper
powershell -ExecutionPolicy Bypass -File .\deploy\windows\run_paper_bot.ps1
```

Ensure `.env` has `PAPER_ONLY=true`, `LIVE_TRADING=false`, and `BAR_CSV_PATH` pointing at the same file as **ExportPath**.

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Compile error | Confirm file is under `Custom\Strategies\`, NT8 updated, F5 rebuild |
| Empty CSV | Strategy enabled? Rithmic connected? MNQ subscription active? |
| `Bar file missing` in Python | Match `ExportPath` and `BAR_CSV_PATH`; create parent folder |
| Stale L2 | Restart strategy after reconnect; check **Market Depth** window in NT8 |
| Wrong symbol | Strategy uses chart instrument — use **MNQ** 1m chart |

## Security

- **Research / paper only** — this exporter does not send orders.
- Do not commit Rithmic credentials; configure them only inside NinjaTrader.

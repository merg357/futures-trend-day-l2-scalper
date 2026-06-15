# Windows VPS deployment — futures-trend-day-l2-scalper

**Research and paper trading only.** Default build does not send live broker orders.

## Live data: NinjaTrader 8 (required for follow mode)

```
NinjaTrader 8 (Rithmic / sim)
    │
    ▼
ScalperL2Exporter strategy (MNQ 1m, OnMarketDepth L1–L5)
    │
    ▼
C:\Bots\futures-trend-day-l2-scalper\data\live\nt8_mnq_1m.csv
    │
    ▼
scalper/paper_runner.py  (--mode follow, BAR_CSV_PATH)
    │
    ├── data/live/signals.jsonl
    ├── data/live/trades.jsonl
    └── data/live/gateway_audit.jsonl
```

Install strategy: `integrations/ninjatrader8/README.md`

**Deprecated for live follow:** `C:\TradeData\futuresbot\live\` recorder path.  
**Historical ETL only:** `FUTURESBOT_ARCHIVE_ROOT` → `scripts/convert_l2_to_bars.py`

## Two hosts

| Host | Role |
|------|------|
| Windows trading VPS (MergMoney) | NT8 + Rithmic + this scalper |
| `187.124.244.78` (Linux) | Jarvis web stack — not NinjaTrader |

## Quick deploy

```powershell
git clone https://github.com/merg357/futures-trend-day-l2-scalper.git C:\Bots\futures-trend-day-l2-scalper
Set-Location C:\Bots\futures-trend-day-l2-scalper
powershell -ExecutionPolicy Bypass -File deploy\windows\MergMoney_deploy.ps1 `
  -SourceRoot "C:\Bots\futures-trend-day-l2-scalper"
```

Then NT8 → compile ScalperL2Exporter → enable on MNQ 1m → start `run_paper_bot.ps1`.

## CSV columns

Required: `timestamp`, `open`, `high`, `low`, `close`, `volume`  
Optional L2: `bid`, `ask`, `bid_size`, `ask_size`, `bid_depth`, `ask_depth`, `delta`

## Safety defaults

| Variable | Default |
|----------|---------|
| `PAPER_ONLY` | `true` |
| `LIVE_TRADING` | `false` |
| `BAR_CSV_PATH` | `...\data\live\nt8_mnq_1m.csv` |
| `NT8_EXPORT_PATH` | same (alias) |

## Docs

| File | Purpose |
|------|---------|
| `CURSOR_VPS_SETUP.md` | Full MergMoney + NT8 setup |
| `CURSOR_AGENT_PROMPT.txt` | One-shot Cursor Agent on VPS |
| `MergMoney_README.md` | Short deploy checklist |
| `integrations/ninjatrader8/README.md` | NT8 strategy install |

## Disclaimer

Past backtest performance does not guarantee future results. **Not connected to a live broker in the default configuration.**

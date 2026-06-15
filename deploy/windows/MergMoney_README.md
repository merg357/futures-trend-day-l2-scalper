# MergMoney Windows VPS — NT8 L2 Paper Bot

**VM:** MergMoney · **Host:** `160.187.32.113` · **Port:** `36936` (RDP)  
**Safety:** `PAPER_ONLY=true` · `LIVE_TRADING=false` — no live broker orders.

---

## Live feed source: NinjaTrader 8 (not futuresbot)

| Source | Use |
|--------|-----|
| **NT8 ScalperL2Exporter** | Live paper follow (`BAR_CSV_PATH`) |
| futuresbot L2 archives | Offline backtest ETL only (`FUTURESBOT_ARCHIVE_ROOT`) |

```
NT8 + Rithmic → ScalperL2Exporter → nt8_mnq_1m.csv → paper_runner follow
```

---

## Connection test (workstation)

| Test | Result |
|------|--------|
| `Test-NetConnection 160.187.32.113 -Port 36936` | TcpTestSucceeded (RDP) |
| SSH on 36936 | Fails — port is RDP |

Deploy **inside the VPS via RDP**.

---

## Step 1 — RDP

1. `mstsc` → `160.187.32.113:36936` → user `tradervps`.

---

## Step 2 — Clone repo on VPS

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
New-Item -ItemType Directory -Path C:\Bots -Force | Out-Null
git clone https://github.com/merg357/futures-trend-day-l2-scalper.git C:\Bots\futures-trend-day-l2-scalper
Set-Location C:\Bots\futures-trend-day-l2-scalper
```

Or copy via RDP drive redirect, then use `-SourceRoot`.

---

## Step 3 — Deploy Python + `.env`

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\MergMoney_deploy.ps1 `
  -SourceRoot "C:\Bots\futures-trend-day-l2-scalper" `
  -InstallRoot "C:\Bots\futures-trend-day-l2-scalper"
```

Creates venv, `data/live`, and `.env` with NT8 CSV paths.

---

## Step 4 — NinjaTrader 8 + ScalperL2Exporter

1. Install NT8; connect **Rithmic** (or sim).
2. Copy strategy:

   ```powershell
   Copy-Item C:\Bots\futures-trend-day-l2-scalper\integrations\ninjatrader8\ScalperL2Exporter.cs `
     "$env:USERPROFILE\Documents\NinjaTrader 8\bin\Custom\Strategies\"
   ```

3. NT8 → NinjaScript Editor → **F5** compile.
4. **MNQ** chart → **1 Minute** → add **ScalperL2Exporter** (enabled).
5. **ExportPath:** `C:\Bots\futures-trend-day-l2-scalper\data\live\nt8_mnq_1m.csv`

Verify during RTH:

```powershell
Get-Content C:\Bots\futures-trend-day-l2-scalper\data\live\nt8_mnq_1m.csv -Tail 3
```

Details: `integrations/ninjatrader8/README.md`

---

## Step 5 — Start paper bot (after NT8 is writing CSV)

```powershell
powershell -ExecutionPolicy Bypass -File C:\Bots\futures-trend-day-l2-scalper\deploy\windows\run_paper_bot.ps1 `
  -InstallRoot "C:\Bots\futures-trend-day-l2-scalper"
```

| Log | Path |
|-----|------|
| Signals | `data\live\signals.jsonl` |
| Trades | `data\live\trades.jsonl` |
| Gateway | `data\live\gateway_audit.jsonl` |

---

## Step 6 — Scheduled task (optional)

```powershell
powershell -ExecutionPolicy Bypass -File C:\Bots\futures-trend-day-l2-scalper\deploy\windows\install_scheduled_task.ps1 `
  -InstallRoot "C:\Bots\futures-trend-day-l2-scalper"
```

Ensure NT8 + strategy start before the task (~09:25).

---

## Quick reference

```powershell
# NT8 must be running first, then:
powershell -ExecutionPolicy Bypass -File C:\Bots\futures-trend-day-l2-scalper\deploy\windows\run_paper_bot.ps1 -InstallRoot C:\Bots\futures-trend-day-l2-scalper
Test-Path C:\Bots\futures-trend-day-l2-scalper\data\live\nt8_mnq_1m.csv
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `Bar file missing` | Enable ScalperL2Exporter; check Rithmic connection |
| Deprecated futuresbot path in `.env` | Use `nt8_mnq_1m.csv` under repo `data\live` |
| No CSV rows | MNQ chart 1m; strategy enabled; market hours |

See `CURSOR_VPS_SETUP.md` for full Cursor + env reference.

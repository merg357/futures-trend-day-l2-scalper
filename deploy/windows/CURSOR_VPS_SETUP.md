# Cursor + Windows VPS setup (MergMoney) — NT8 L2 paper scalper

Complete guide for **Windows Server 2022** trading VPS. **Paper mode only** (`PAPER_ONLY=true`).

| Item | Value |
|------|--------|
| VPS provider | MergMoney |
| RDP host | `160.187.32.113` |
| RDP port | `36936` |
| RDP user | `tradervps` |
| Password | **Enter at login — never store in repo** |
| Install root | `C:\Bots\futures-trend-day-l2-scalper` |
| GitHub repo | `https://github.com/merg357/futures-trend-day-l2-scalper.git` |

---

## Architecture (NT8-first)

```
NinjaTrader 8 + Rithmic (or sim)
  → ScalperL2Exporter strategy (MNQ 1m chart)
  → OnMarketDepth L1–L5 + OnMarketData (native NT8 L2 stream)
  → append CSV: C:\Bots\futures-trend-day-l2-scalper\data\live\nt8_mnq_1m.csv
  → Python paper_runner.py --mode follow (polls BAR_CSV_PATH)
  → data/live/signals.jsonl, trades.jsonl (paper only)
```

**Do not** use `C:\TradeData\futuresbot\live\` for live paper follow. That path is deprecated.  
`FUTURESBOT_ARCHIVE_ROOT` is for **offline historical ETL** only (`scripts/convert_l2_to_bars.py`).

---

## 1. Connect to the VPS (RDP)

1. `Win+R` → `mstsc` → Enter.
2. **Computer:** `160.187.32.113:36936`
3. **User:** `tradervps`
4. Optional: **Local Resources → More** → enable a local drive for file copy via `\\tsclient\D\...`

Port `36936` is RDP, not SSH. Run everything **inside** the VPS session.

---

## 2. Install Cursor (optional)

1. Edge → [cursor.com/download](https://cursor.com/download) → Windows x64.
2. **File → Open Folder** → `C:\Bots\futures-trend-day-l2-scalper` (after clone).

---

## 3. Install Git, Python 3.12, NinjaTrader 8

Open **PowerShell** (Administrator recommended):

```powershell
winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements
winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
git --version
py -3.12 --version
```

Install **NinjaTrader 8** from [ninjatrader.com](https://ninjatrader.com/) and configure **Rithmic** (or sim) under **Connections**. Credentials live in NT8 only — not in `.env`.

---

## 4. Clone repo

```powershell
New-Item -ItemType Directory -Path C:\Bots -Force | Out-Null
Set-Location C:\Bots
git clone https://github.com/merg357/futures-trend-day-l2-scalper.git futures-trend-day-l2-scalper
Set-Location C:\Bots\futures-trend-day-l2-scalper
git pull
```

---

## 5. Install ScalperL2Exporter in NinjaTrader 8

```powershell
$nt8 = Join-Path $env:USERPROFILE "Documents\NinjaTrader 8\bin\Custom\Strategies"
New-Item -ItemType Directory -Path $nt8 -Force | Out-Null
Copy-Item C:\Bots\futures-trend-day-l2-scalper\integrations\ninjatrader8\ScalperL2Exporter.cs $nt8
```

In NT8:

1. **New → NinjaScript Editor** → **F5** (Compile).
2. Open **MNQ** chart → **1 Minute**.
3. Connect **Rithmic** (green connection).
4. Chart → **Strategies** → **ScalperL2Exporter** → Enable.
5. **ExportPath:** `C:\Bots\futures-trend-day-l2-scalper\data\live\nt8_mnq_1m.csv`

Verify CSV grows:

```powershell
New-Item -ItemType Directory -Force -Path C:\Bots\futures-trend-day-l2-scalper\data\live | Out-Null
Get-Content C:\Bots\futures-trend-day-l2-scalper\data\live\nt8_mnq_1m.csv -Tail 3 -ErrorAction SilentlyContinue
```

Full NT8 steps: `integrations/ninjatrader8/README.md`

---

## 6. Create `.env`

```powershell
Copy-Item .env.example .env
notepad .env
```

### Key variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PAPER_ONLY` | `true` | Block live orders |
| `LIVE_TRADING` | `false` | Gateway stub |
| `BAR_CSV_PATH` | `C:\Bots\futures-trend-day-l2-scalper\data\live\nt8_mnq_1m.csv` | NT8 CSV (follow mode) |
| `NT8_EXPORT_PATH` | same as above | Alias; used if `BAR_CSV_PATH` empty |
| `SCALPER_CONFIG` | `configs/production/mnq_walkforward_optimized.yaml` | Strategy YAML |
| `LIVE_LOG_DIR` | `data/live` | signals.jsonl, trades.jsonl |
| `RUNNER_MODE` | `follow` | Tail NT8 CSV |
| `POLL_SECONDS` | `2` | CSV poll interval |
| `FUTURESBOT_ARCHIVE_ROOT` | *(optional)* | **Historical ETL only** — not live feed |

Recommended paper `.env`:

```ini
PAPER_ONLY=true
LIVE_TRADING=false
LIVE_TRADING_CONFIRM=

DATA_DIR=data
REPORTS_DIR=data/reports
LIVE_LOG_DIR=data/live
SCALPER_CONFIG=configs/production/mnq_walkforward_optimized.yaml
BAR_CSV_PATH=C:\Bots\futures-trend-day-l2-scalper\data\live\nt8_mnq_1m.csv
NT8_EXPORT_PATH=C:\Bots\futures-trend-day-l2-scalper\data\live\nt8_mnq_1m.csv

RUNNER_MODE=follow
POLL_SECONDS=2
LOG_LEVEL=INFO
```

---

## 7. Python venv

```powershell
Set-Location C:\Bots\futures-trend-day-l2-scalper
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -c "import scalper; print('scalper ok')"
python -m pytest tests -q
```

Or automated:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\MergMoney_deploy.ps1 `
  -SourceRoot "C:\Bots\futures-trend-day-l2-scalper" `
  -InstallRoot "C:\Bots\futures-trend-day-l2-scalper"
```

---

## 8. Start order (important)

1. **NinjaTrader 8** connected to Rithmic/sim.
2. **ScalperL2Exporter** enabled on MNQ 1m chart.
3. Confirm `nt8_mnq_1m.csv` has recent rows.
4. **Then** start Python paper runner:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
Set-Location C:\Bots\futures-trend-day-l2-scalper
powershell -ExecutionPolicy Bypass -File .\deploy\windows\run_paper_bot.ps1 `
  -InstallRoot "C:\Bots\futures-trend-day-l2-scalper"
```

Expected:

- `Paper mode active (PAPER_ONLY=true, LIVE_TRADING=false)`
- `Following C:\Bots\futures-trend-day-l2-scalper\data\live\nt8_mnq_1m.csv`

Stop with **Ctrl+C**.

### Replay (offline, no NT8)

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\run_paper_bot.ps1 `
  -InstallRoot "C:\Bots\futures-trend-day-l2-scalper" `
  -Mode replay `
  -DataPath "C:\Bots\futures-trend-day-l2-scalper\data\raw\YOUR_FILE.csv"
```

---

## 9. Verify

```powershell
Get-Content C:\Bots\futures-trend-day-l2-scalper\data\live\signals.jsonl -Tail 5
Get-Content C:\Bots\futures-trend-day-l2-scalper\data\live\trades.jsonl -Tail 5
Get-Item C:\Bots\futures-trend-day-l2-scalper\data\live\nt8_mnq_1m.csv | Select-Object Length, LastWriteTime
```

---

## 10. Optional scheduled task

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\install_scheduled_task.ps1 `
  -InstallRoot "C:\Bots\futures-trend-day-l2-scalper"
```

Start NT8 + strategy **before** the task fires (~09:25).

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Bar file missing` | Enable ScalperL2Exporter; Rithmic connected; paths match |
| Deprecated futuresbot warning | Update `.env` to `nt8_mnq_1m.csv` |
| NT8 compile error | File in `Custom\Strategies\`; F5 rebuild |
| No signals | Normal in chop; check `signals.jsonl` during RTH |
| `Could not read bars` | Fix CSV header; no partial rows |

---

## 11. Cursor Agent one-shot

Paste `deploy/windows/CURSOR_AGENT_PROMPT.txt` into Cursor Agent on the VPS.

---

## Security

- Never commit `.env` or Rithmic passwords.
- Keep `PAPER_ONLY=true` until paper logs match backtests.
- Linux VPS `187.124.244.78` is a separate Jarvis host — not this Windows bot.

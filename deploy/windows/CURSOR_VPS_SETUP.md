# Cursor + Windows VPS setup (MergMoney) — futures-trend-day-l2-scalper

Complete guide for **Windows Server 2022** trading VPS. Paper mode only by default.

| Item | Value |
|------|--------|
| VPS provider | MergMoney |
| RDP host | `160.187.32.113` |
| RDP port | `36936` |
| RDP user | `tradervps` |
| Password | **Enter at login — never store in repo or this file** |
| Install root (recommended) | `C:\Bots\futures-trend-day-l2-scalper` |
| GitHub repo | `https://github.com/merg357/futures-trend-day-l2-scalper.git` |

---

## 1. Connect to the VPS (RDP)

1. On your PC: `Win+R` → `mstsc` → Enter.
2. **Computer:** `160.187.32.113:36936`
3. **User name:** `tradervps`
4. Use your VPS password (not committed to git).
5. Optional: **Show Options** → **Local Resources** → **More** → check a local drive (e.g. `D:`) to copy files via `\\tsclient\D\...`

**Note:** Port `36936` is RDP, not SSH. Deploy and run everything **inside** the VPS session.

---

## 2. Install Cursor on the VPS (if missing)

1. Open **Microsoft Edge** on the VPS.
2. Download: [https://cursor.com/download](https://cursor.com/download) → **Windows** (x64).
3. Run the installer (per-user install is fine).
4. Sign in to Cursor (same account as your dev machine if you want synced settings).
5. **File → Open Folder** → you will open `C:\Bots\futures-trend-day-l2-scalper` after clone (step 4).

---

## 3. Install Git and Python 3.11+

Open **PowerShell** (Run as Administrator recommended for winget).

### 3.1 Git

```powershell
winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements
```

Close and reopen PowerShell, then:

```powershell
git --version
```

### 3.2 Python 3.12 (recommended)

```powershell
winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
```

Verify:

```powershell
py -3.12 --version
py -3.12 -m pip --version
```

If `py` is missing, use `python --version` after adding Python to PATH during install.

### 3.3 Execution policy (session only — safe)

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
```

---

## 4. Clone from GitHub

```powershell
New-Item -ItemType Directory -Path C:\Bots -Force | Out-Null
Set-Location C:\Bots
git clone https://github.com/merg357/futures-trend-day-l2-scalper.git futures-trend-day-l2-scalper
Set-Location C:\Bots\futures-trend-day-l2-scalper
git status
```

---

## 5. Directory layout after clone

```
C:\Bots\futures-trend-day-l2-scalper\
├── .env.example          # Template — copy to .env (never commit .env)
├── .gitignore
├── pyproject.toml
├── requirements.txt
├── README.md
├── configs\
│   ├── mnq_default.yaml
│   ├── mes_default.yaml
│   └── production\
│       └── mnq_walkforward_optimized.yaml   # Production paper config
├── scalper\              # Core library (backtest, paper_runner, safety)
├── scripts\              # Backtest / optimization CLIs
├── tests\
├── deploy\
│   └── windows\
│       ├── install.ps1
│       ├── MergMoney_deploy.ps1
│       ├── run_paper_bot.ps1
│       ├── install_scheduled_task.ps1
│       ├── CURSOR_VPS_SETUP.md
│       └── CURSOR_AGENT_PROMPT.txt
└── data\
    ├── raw\              # Large CSVs gitignored — backtest copies here
    ├── live\             # Runtime: signals.jsonl, trades.jsonl
    └── sample\           # Small sample data (if present)
```

External paths (not in repo):

```
C:\TradeData\futuresbot\live\MNQ_1m_live.csv   # NinjaTrader / futuresbot live bars
```

---

## 6. Create `.env` from `.env.example`

```powershell
Copy-Item .env.example .env
notepad .env
```

### 6.1 Every environment variable

| Variable | Example / default | Required | Purpose |
|----------|-------------------|----------|---------|
| **PAPER_ONLY** | `true` | Yes | `true` = log signals, block live orders. Only set `false` after validation. |
| **LIVE_TRADING** | `false` | Yes | Must be `true` for any live gateway path (stub still blocks by default). |
| **LIVE_TRADING_CONFIRM** | *(empty)* | If live | Must be exactly `I_UNDERSTAND_RISK` when `PAPER_ONLY=false` and `LIVE_TRADING=true`. |
| **DATA_DIR** | `data` | No | Base data folder name (documentation / scripts). |
| **REPORTS_DIR** | `data/reports` | No | Backtest report output directory. |
| **LIVE_LOG_DIR** | `data/live` | Yes | Folder for `signals.jsonl`, `trades.jsonl`, `gateway_audit.jsonl`. |
| **SCALPER_CONFIG** | `configs/production/mnq_walkforward_optimized.yaml` | Yes | Strategy YAML (relative to repo root). Alias some docs call **CONFIG_PATH** — use **SCALPER_CONFIG** in this project. |
| **BAR_CSV_PATH** | `C:\TradeData\futuresbot\live\MNQ_1m_live.csv` | Yes | Growing 1m CSV from NinjaTrader L2 recorder (follow mode). |
| **FUTURESBOT_ARCHIVE_ROOT** | `C:\TradeData\StorageBox\bundles\futuresbot\archives\l2` | No | Offline L2 tarball ETL only (scalper/l2_etl.py). |
| **RUNNER_MODE** | `follow` | Yes | `follow` = tail live CSV; `replay` = one-shot historical file. |
| **POLL_SECONDS** | `2` | No | Seconds between CSV polls in follow mode. |
| **LOG_LEVEL** | `INFO` | No | Python logging: DEBUG, INFO, WARNING, ERROR. |
| **VPS_SSH_HOST** | `187.124.244.78` | No | **Different host** (Linux Jarvis) — ETL/sync docs only, not this Windows bot. |
| **VPS_SSH_USER** | `root` | No | Linux VPS SSH user. |
| **VPS_SSH_IDENTITY_FILE** | `%USERPROFILE%\.ssh\hostinger_vps` | No | SSH key path on workstation. |
| **RITHMIC_USER** | *(empty)* | No | Leave empty until live gateway implemented. |
| **RITHMIC_PASSWORD** | *(empty)* | No | Never commit. |
| **RITHMIC_SYSTEM** | *(empty)* | No | Broker system name. |
| **NINJATRADER_ACCOUNT** | *(empty)* | No | Account id for future live bridge. |

### 6.2 Derived log paths (not separate env vars)

The runner writes under `LIVE_LOG_DIR`:

| File | Env alias (informal) | Path |
|------|----------------------|------|
| Entry signals | SIGNAL_LOG_PATH | `{LIVE_LOG_DIR}/signals.jsonl` |
| Paper trades | TRADE_LOG_PATH | `{LIVE_LOG_DIR}/trades.jsonl` |
| Gateway audit | — | `{LIVE_LOG_DIR}/gateway_audit.jsonl` |
| Replay events | — | `{LIVE_LOG_DIR}/runner_events.jsonl` |

### 6.3 Recommended MergMoney `.env` (paper)

```ini
PAPER_ONLY=true
LIVE_TRADING=false
LIVE_TRADING_CONFIRM=

DATA_DIR=data
REPORTS_DIR=data/reports
LIVE_LOG_DIR=data/live
SCALPER_CONFIG=configs/production/mnq_walkforward_optimized.yaml
BAR_CSV_PATH=C:\TradeData\futuresbot\live\MNQ_1m_live.csv
FUTURESBOT_ARCHIVE_ROOT=C:\TradeData\StorageBox\bundles\futuresbot\archives\l2

RUNNER_MODE=follow
POLL_SECONDS=2
LOG_LEVEL=INFO
```

---

## 7. Python virtual environment and dependencies

From repo root `C:\Bots\futures-trend-day-l2-scalper`:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -c "import scalper; print('scalper ok')"
python -m pytest tests -q
```

---

## 8. Create required folders

```powershell
New-Item -ItemType Directory -Force -Path 
  "C:\TradeData\futuresbot\live", 
  "C:\Bots\futures-trend-day-l2-scalper\data\live", 
  "C:\Bots\futures-trend-day-l2-scalper\data\raw" | Out-Null
```

Or use automated install:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\MergMoney_deploy.ps1 
  -SourceRoot "C:\Bots\futures-trend-day-l2-scalper" 
  -InstallRoot "C:\Bots\futures-trend-day-l2-scalper"
```

---

## 9. NinjaTrader 8 + Rithmic + CSV export

1. Install **NinjaTrader 8** on the VPS.
2. **Connections** → configure **Rithmic** (credentials in NinjaTrader only — not in `.env` for paper follow).
3. Open an **MNQ** chart (1-minute) with your data feed connected during RTH.
4. Run your **futuresbot L2 recorder** (or NT strategy) that appends aggregated 1m rows to:

   `C:\TradeData\futuresbot\live\MNQ_1m_live.csv`

5. **CSV columns (must match backtester):**

   - Required: `timestamp`, `open`, `high`, `low`, `close`, `volume`
   - Optional L2: `bid`, `ask`, `bid_size`, `ask_size`, `bid_depth`, `ask_depth`, `delta`

6. Timestamps: use **US/Eastern** session consistency with your backtest CSVs.
7. Before starting the bot:

   ```powershell
   Get-Item C:\TradeData\futuresbot\live\MNQ_1m_live.csv | Select-Object Length, LastWriteTime
   Get-Content C:\TradeData\futuresbot\live\MNQ_1m_live.csv -Tail 3
   ```

---

## 10. Run paper bot (exact PowerShell)

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
Set-Location C:\Bots\futures-trend-day-l2-scalper
powershell -ExecutionPolicy Bypass -File .\deploy\windows\run_paper_bot.ps1 
  -InstallRoot "C:\Bots\futures-trend-day-l2-scalper"
```

Expected output includes:

- `Paper mode active (PAPER_ONLY=true, LIVE_TRADING=false)`
- `Starting paper runner: mode=follow`
- `Following C:\TradeData\futuresbot\live\MNQ_1m_live.csv`

Stop with **Ctrl+C**.

### Replay mode (offline test)

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\run_paper_bot.ps1 
  -InstallRoot "C:\Bots\futures-trend-day-l2-scalper" 
  -Mode replay 
  -DataPath "C:\Bots\futures-trend-day-l2-scalper\data\raw\YOUR_FILE.csv"
```

---

## 11. Optional: Windows Scheduled Task

After manual validation:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Bots\futures-trend-day-l2-scalper\deploy\windows\install_scheduled_task.ps1 
  -InstallRoot "C:\Bots\futures-trend-day-l2-scalper"
```

Or during deploy:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\MergMoney_deploy.ps1 
  -SourceRoot "C:\Bots\futures-trend-day-l2-scalper" 
  -InstallRoot "C:\Bots\futures-trend-day-l2-scalper" 
  -RegisterScheduledTask
```

Task name: **MNQ-Paper-Scalper** (daily ~09:25). Stop task at session end if needed.

---

## 12. Verify the bot is working

1. **Console:** no repeated `Bar file missing` after CSV exists; `Following ...` line present.
2. **Signals log:**

   ```powershell
   Get-Content C:\Bots\futures-trend-day-l2-scalper\data\live\signals.jsonl -Tail 5
   ```

   Each line is JSON with `side`, `trend_score`, `l2_score`, `paper_only: true`.

3. **Trades log** (after exits):

   ```powershell
   Get-Content C:\Bots\futures-trend-day-l2-scalper\data\live\trades.jsonl -Tail 5
   ```

4. **Gateway audit** (should show blocked/paper behavior):

   ```powershell
   Get-Content C:\Bots\futures-trend-day-l2-scalper\data\live\gateway_audit.jsonl -Tail 5
   ```

5. **CSV still growing** during session (`LastWriteTime` updates).

---

## 13. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|----------------|-----|
| `Python venv missing` | No `.venv` | Run `install.ps1` or section 7 |
| `cannot be loaded because running scripts is disabled` | Execution policy | `Set-ExecutionPolicy -Scope Process Bypass` |
| `BAR_CSV_PATH not set` | Missing `.env` | Copy `.env.example` → `.env` |
| `Bar file missing` | NT recorder not running | Start recorder; create folder `C:\TradeData\futuresbot\live` |
| `Could not read bars` | Bad CSV format | Fix headers; ensure UTF-8 CSV, no partial rows |
| No new signals | Low activity / chop filters | Normal; check `LOG_LEVEL=DEBUG` temporarily |
| `py` not found | Python not on PATH | Reinstall Python; check "Add to PATH" |
| Wrong config path | `SCALPER_CONFIG` typo | Use `configs/production/mnq_walkforward_optimized.yaml` |
| Empty `signals.jsonl` | No entries yet | Wait for strategy conditions during RTH |

---

## 14. One-shot Cursor Agent prompt

Paste the contents of `deploy/windows/CURSOR_AGENT_PROMPT.txt` into **Cursor Agent** on the VPS to automate steps 3–10.

---

## Security

- Rotate VPS password if shared in chat.
- Never commit `.env`, passwords, or API keys.
- Keep `PAPER_ONLY=true` until paper logs match backtests.
- Linux VPS `187.124.244.78` is a **separate** Jarvis host — not used to run this Windows scalper.


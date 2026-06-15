# MergMoney Windows VPS — Paper Bot Deploy

**VM:** MergMoney · **Host:** `160.187.32.113` · **Port:** `36936` (RDP)  
**Safety:** `PAPER_ONLY=true` and `LIVE_TRADING=false` — no live broker orders.

---

## Connection test results (from workstation)

| Test | Result |
|------|--------|
| `Test-NetConnection 160.187.32.113 -Port 36936` | **TcpTestSucceeded: True** |
| ICMP ping | Failed (common on VPS — ignore if TCP works) |
| SSH on port 36936 | **Failed** — connection reset (port is RDP, not OpenSSH) |

**Conclusion:** Deploy must be done **inside the VPS via RDP**. Remote SSH/SCP from this PC is not available on port 36936.

---

## Step 1 — Connect via RDP

1. Open Remote Desktop Connection (`Win+R` → `mstsc`).
2. **Computer:** `160.187.32.113:36936`
3. **User:** `tradervps`
4. Enter your VPS password when prompted (never save it in repo files).

Optional — save a `.rdp` file locally (password stored only in Windows Credential Manager, not in git):

```
full address:s:160.187.32.113:36936
username:s:tradervps
```

---

## Step 2 — Copy project to the VPS

The scalper is not yet a standalone git repo on GitHub. Use one of:

### Option A — RDP drive redirect (recommended)

1. In `mstsc`, **Local Resources → More** → enable your dev drive (e.g. `D:`).
2. On the VPS, copy from `\\tsclient\D\AI_Vault\workspace\jarvis-one\futures-trend-day-l2-scalper` to:

   `C:\Temp\futures-trend-day-l2-scalper`

### Option B — Zip transfer

1. Zip `futures-trend-day-l2-scalper` on your PC (exclude `.venv`, `__pycache__`, `.env`).
2. Copy zip via RDP clipboard/USB/cloud, extract to `C:\Temp\futures-trend-day-l2-scalper`.

### Option C — Git clone (when published)

```powershell
git clone --depth 1 <your-repo-url> C:\Temp\jarvis-one
# then use -SourceRoot C:\Temp\jarvis-one\futures-trend-day-l2-scalper
```

---

## Step 3 — Run one-shot install (on VPS)

Open **PowerShell as Administrator** on the VPS:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force

cd C:\Temp\futures-trend-day-l2-scalper

powershell -ExecutionPolicy Bypass -File .\deploy\windows\MergMoney_deploy.ps1 `
  -SourceRoot "C:\Temp\futures-trend-day-l2-scalper" `
  -InstallRoot "C:\Bots\futures-trend-day-l2-scalper"
```

This will:

- Install Python 3.12 via winget if missing
- Create `C:\TradeData\futuresbot\live\`
- Install bot to `C:\Bots\futures-trend-day-l2-scalper`
- Create `.env` with paper safety flags and production config path
- **Not** write any VPS password to disk

Production config used: `configs/production/mnq_walkforward_optimized.yaml`

---

## Step 4 — NinjaTrader / futuresbot CSV path

Point your L2 recorder to append 1-minute bars here:

```
C:\TradeData\futuresbot\live\MNQ_1m_live.csv
```

Required columns: `timestamp`, `open`, `high`, `low`, `close`, `volume`  
Optional L2: `bid`, `ask`, `bid_size`, `ask_size`, `bid_depth`, `ask_depth`, `delta`

Verify the file grows during RTH before starting the bot.

---

## Step 5 — Start paper bot (manual first)

```powershell
powershell -ExecutionPolicy Bypass -File C:\Bots\futures-trend-day-l2-scalper\deploy\windows\run_paper_bot.ps1 `
  -InstallRoot "C:\Bots\futures-trend-day-l2-scalper"
```

Expected console output includes:

```
Paper mode active (PAPER_ONLY=true, LIVE_TRADING=false)
Starting paper runner: mode=follow symbol-config=...\mnq_walkforward_optimized.yaml
```

Monitor logs:

| File | Purpose |
|------|---------|
| `C:\Bots\futures-trend-day-l2-scalper\data\live\signals.jsonl` | Entry signals |
| `C:\Bots\futures-trend-day-l2-scalper\data\live\trades.jsonl` | Paper exits |
| `C:\Bots\futures-trend-day-l2-scalper\data\live\gateway_audit.jsonl` | Blocked orders |

Stop with `Ctrl+C`.

Replay test (offline, no live CSV needed):

```powershell
powershell -ExecutionPolicy Bypass -File C:\Bots\futures-trend-day-l2-scalper\deploy\windows\run_paper_bot.ps1 `
  -InstallRoot "C:\Bots\futures-trend-day-l2-scalper" `
  -Mode replay `
  -DataPath "C:\Bots\futures-trend-day-l2-scalper\data\raw\MNQ_20260508_1m.csv"
```

---

## Step 6 — Scheduled task (optional, after manual validation)

```powershell
powershell -ExecutionPolicy Bypass -File C:\Bots\futures-trend-day-l2-scalper\deploy\windows\install_scheduled_task.ps1 `
  -InstallRoot "C:\Bots\futures-trend-day-l2-scalper"
```

Or register during deploy:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\MergMoney_deploy.ps1 `
  -SourceRoot "C:\Temp\futures-trend-day-l2-scalper" `
  -RegisterScheduledTask
```

Task name: `MNQ-Paper-Scalper` · Daily 09:25 · Runs `run_paper_bot.ps1`

Remove task:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Bots\futures-trend-day-l2-scalper\deploy\windows\install_scheduled_task.ps1 -Unregister
```

### NSSM service (advanced, not required)

If you prefer a Windows service instead of Task Scheduler:

1. Download [NSSM](https://nssm.cc/download) to `C:\Tools\nssm\nssm.exe`.
2. Service command:

   ```
   powershell.exe -ExecutionPolicy Bypass -NoProfile -File C:\Bots\futures-trend-day-l2-scalper\deploy\windows\run_paper_bot.ps1 -InstallRoot C:\Bots\futures-trend-day-l2-scalper
   ```

3. Set startup type Manual until paper logs look correct for several sessions.

---

## What was deployed remotely vs manually

| Action | From workstation | On VPS (RDP) |
|--------|------------------|--------------|
| TCP port test | Done — port open | — |
| SSH deploy | **Not possible** (RDP port) | — |
| Copy project | — | **You** (RDP drive/zip) |
| `MergMoney_deploy.ps1` | Created in repo | **You** run it |
| `.env` paper config | Template in script | Created on VPS |
| Start paper bot | — | **You** run `run_paper_bot.ps1` |
| Scheduled task | — | Optional, after validation |

---

## Security reminders

1. **Rotate the VPS password** after first RDP login if it was shared in chat.
2. **Do not commit** `.env`, passwords, or RDP credentials to git.
3. For repeat access, prefer **SSH key auth on a dedicated admin port** if your provider adds OpenSSH later — keep RDP restricted by firewall.
4. Keep `PAPER_ONLY=true` until you have independently validated paper logs vs backtests.
5. `LIVE_TRADING=false` — the default gateway stub does not send real orders even if flags change.

---

## Quick reference — exact commands after RDP login

```powershell
# 1. Copy project to C:\Temp\futures-trend-day-l2-scalper (via RDP drive), then:
Set-ExecutionPolicy -Scope Process Bypass -Force
cd C:\Temp\futures-trend-day-l2-scalper
powershell -ExecutionPolicy Bypass -File .\deploy\windows\MergMoney_deploy.ps1 -SourceRoot "C:\Temp\futures-trend-day-l2-scalper"

# 2. Confirm NinjaTrader CSV exists / will be written:
Test-Path C:\TradeData\futuresbot\live\MNQ_1m_live.csv

# 3. Start paper bot:
powershell -ExecutionPolicy Bypass -File C:\Bots\futures-trend-day-l2-scalper\deploy\windows\run_paper_bot.ps1 -InstallRoot "C:\Bots\futures-trend-day-l2-scalper"
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `BAR_CSV_PATH not set` | Ensure `.env` exists; re-run deploy or set path in `.env` |
| CSV not updating | Start NinjaTrader L2 recorder; confirm path matches `BAR_CSV_PATH` |
| Python missing | Install 3.12 from python.org or re-run deploy (winget) |
| RDP cannot connect | Confirm firewall allows 36936; verify IP/port with provider |

See also: [deploy/windows/README.md](./README.md) for architecture and Linux VPS notes (`187.124.244.78` is a different host).

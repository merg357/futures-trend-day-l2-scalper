# Windows VPS deployment — futures-trend-day-l2-scalper

**Research and paper trading only.** This package does not connect to a live broker and does not place real orders unless you explicitly disable paper mode and implement the broker adapter in `scalper/live_gateway.py`.

## Important: two different hosts

| Host | OS | Role in this project |
|------|-----|----------------------|
| `187.124.244.78` (Hostinger) | **Ubuntu 24.04** | Jarvis/respondermade web stack, Docker, PM2 — see `jarvis-one/respondermade/docs/vps-audit.md` |
| **Windows trading VPS / mini PC** | **Windows Server or Win 10/11** | NinjaTrader + Rithmic + futuresbot L2 recorder + this scalper (paper follow) |

The scalper bot belongs on the **Windows** machine where NinjaTrader and `C:\TradeData\futuresbot\` already live. The Linux VPS at `187.124.244.78` is not a NinjaTrader host.

## Quick deploy (on Windows VPS)

```powershell
# 1. Copy or clone repo to the VPS, then from repo root:
powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 `
  -InstallRoot "C:\Bots\futures-trend-day-l2-scalper"

# 2. Edit paths (no secrets in git):
notepad C:\Bots\futures-trend-day-l2-scalper\.env

# 3. Run paper bot (follows growing CSV from NinjaTrader):
powershell -ExecutionPolicy Bypass -File deploy\windows\run_paper_bot.ps1 `
  -InstallRoot "C:\Bots\futures-trend-day-l2-scalper"
```

Replay mode (offline validation on VPS):

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\run_paper_bot.ps1 `
  -InstallRoot "C:\Bots\futures-trend-day-l2-scalper" `
  -Mode replay `
  -DataPath "C:\Bots\futures-trend-day-l2-scalper\data\raw\MNQ_20260508_1m.csv"
```

## Safety defaults

| Variable | Default | Meaning |
|----------|---------|---------|
| `PAPER_ONLY` | `true` | Signals logged; orders blocked |
| `LIVE_TRADING` | `false` | Broker gateway stub |
| `LIVE_TRADING_CONFIRM` | *(empty)* | Must be `I_UNDERSTAND_RISK` for any live path |

`scalper/live_gateway.py` audits blocked orders to `data/live/gateway_audit.jsonl`. Even with live flags set, the default build returns `not_implemented` — wire Rithmic/NinjaTrader only after independent validation.

## NinjaTrader integration path

```
NinjaTrader 8 (Rithmic connection)
    │
    ▼
L2 recorder / futuresbot export  ──►  C:\TradeData\futuresbot\live\MNQ_1m_live.csv
    │                                      (timestamp, OHLCV, bid/ask, L2 depth, delta)
    ▼
scalper/paper_runner.py  (--mode follow)
    │
    ├── data/live/signals.jsonl   (entry signals)
    ├── data/live/trades.jsonl    (paper exits / PnL)
    └── data/live/gateway_audit.jsonl
```

### CSV format (required columns)

- Required: `timestamp`, `open`, `high`, `low`, `close`, `volume`
- Optional L2: `bid`, `ask`, `bid_size`, `ask_size`, `bid_depth`, `ask_depth`, `delta`

ETL from archived L2 tarballs (offline) uses `scalper/l2_etl.py` and paths like:

`C:\TradeData\StorageBox\bundles\futuresbot\archives\l2\MNQ\*.tar.gz`

### NinjaTrader setup checklist (manual)

1. Install NinjaTrader 8 + Rithmic data feed on the Windows VPS.
2. Run your existing **futuresbot** L2 recorder (or NT strategy) to append 1-minute bars with L2 fields to `BAR_CSV_PATH`.
3. Ensure the CSV uses America/New_York session timestamps consistent with backtests.
4. Start `run_paper_bot.ps1` during RTH; monitor `data/live/signals.jsonl`.
5. Compare paper signals to `scripts/run_backtest.py` on the same CSV before any live consideration.

## Production config

Validated walk-forward params:

`configs/production/mnq_walkforward_optimized.yaml`

(Copy of `configs/mnq_walkforward_optimized.yaml` — holdout results in `data/reports/recommended_tests/RECOMMENDED_TESTS_REPORT.md`.)

## Run as a scheduled task (optional)

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-ExecutionPolicy Bypass -File C:\Bots\futures-trend-day-l2-scalper\deploy\windows\run_paper_bot.ps1"
$trigger = New-ScheduledTaskTrigger -Daily -At "09:25"
Register-ScheduledTask -TaskName "MNQ-Paper-Scalper" -Action $action -Trigger $trigger `
  -Description "Paper-only L2 scalper follow mode"
```

Stop the task at session end or run under a process supervisor you already use on the Windows box.

## SSH to Linux VPS (`187.124.244.78`)

Used for Jarvis/respondermade deploy (`respondermade/scripts/deploy-vps.sh`), **not** for this Windows bot.

Current blocker (2026-06-15): **host key changed** — SSH fails until `known_hosts` is updated.

Fix on your workstation:

```powershell
# Remove stale key (offending line 4 per last test)
ssh-keygen -R 187.124.244.78

# Re-accept host key and test with Hostinger key
ssh -i $env:USERPROFILE\.ssh\hostinger_vps -o StrictHostKeyChecking=accept-new root@187.124.244.78 "hostname"
```

Add to `~/.ssh/config` for convenience:

```
Host hostinger-vps
  HostName 187.124.244.78
  User root
  IdentityFile ~/.ssh/hostinger_vps
```

## Existing workspace patterns

| Pattern | Location | Notes |
|---------|----------|-------|
| Linux VPS deploy | `respondermade/scripts/deploy-vps.sh` | PM2 + Next.js on Ubuntu |
| VPS audit | `respondermade/docs/vps-audit.md` | `187.124.244.78` runtime inventory |
| futuresbot L2 archives | `D:\TradeData\...\futuresbot\archives\l2` | Used by `scalper/l2_etl.py` |
| Windows mini PC | `jarvis-one/deploy/README-MINI-PC.md` | Task Scheduler, not NinjaTrader |
| PM2 on VPS | `respondermade/ecosystem.config.cjs` | Node apps only |

There is **no** existing NinjaTrader→Python live bridge in this repo; this deploy package adds the paper follow path only.

## Logs

| File | Content |
|------|---------|
| `data/live/signals.jsonl` | Entry signals with trend/L2 scores |
| `data/live/trades.jsonl` | Paper trade exits |
| `data/live/gateway_audit.jsonl` | Blocked/not-implemented order attempts |
| `data/live/runner_events.jsonl` | Replay summaries |

## Disclaimer

Past backtest performance does not guarantee future results. This software is for research. **Not connected to a live broker in the default configuration.**

#Requires -Version 5.1
<#
.SYNOPSIS
  One-shot deploy for MergMoney Windows VPS (paper trading only).

.DESCRIPTION
  Installs Python (if missing), copies/clones futures-trend-day-l2-scalper,
  creates venv, writes .env with PAPER_ONLY=true, and optionally registers
  a Scheduled Task. Never stores VPS or broker passwords.

.PARAMETER InstallRoot
  Bot install directory (default: C:\Bots\futures-trend-day-l2-scalper)

.PARAMETER SourceRoot
  Local folder containing the repo (skip git clone). Use after copying via RDP.

.PARAMETER GitRepo
  Optional git URL. Clones into a temp dir and copies the scalper subfolder.

.PARAMETER RegisterScheduledTask
  Register daily MNQ-Paper-Scalper task at 09:25 ET (manual start recommended first).

.EXAMPLE
  # After copying repo to C:\Temp\futures-trend-day-l2-scalper via RDP:
  powershell -ExecutionPolicy Bypass -File C:\Temp\futures-trend-day-l2-scalper\deploy\windows\MergMoney_deploy.ps1 `
    -SourceRoot "C:\Temp\futures-trend-day-l2-scalper"
#>
param(
    [string]$InstallRoot = "C:\Bots\futures-trend-day-l2-scalper",
    [string]$SourceRoot = "",
    [string]$GitRepo = "",
    [switch]$RegisterScheduledTask
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectName = "futures-trend-day-l2-scalper"
$BarCsvPath = "C:\Bots\futures-trend-day-l2-scalper\data\live\nt8_mnq_1m.csv"
$Nt8ExportPath = $BarCsvPath
$LiveDataDir = "C:\Bots\futures-trend-day-l2-scalper\data\live"
$ArchiveRoot = "C:\TradeData\StorageBox\bundles\futuresbot\archives\l2"
$ProductionConfig = "configs/production/mnq_walkforward_optimized.yaml"

Write-Host "=== MergMoney VPS deploy (PAPER ONLY) ===" -ForegroundColor Cyan
Write-Host "Target: $InstallRoot"
Write-Host "PAPER_ONLY=true / LIVE_TRADING=false (enforced in .env)" -ForegroundColor Green

function Test-Python312 {
    try {
        $v = & py -3.12 -c "import sys; print(sys.version)" 2>$null
        if ($v) { return $true }
    } catch {}
    try {
        $v = & python -c "import sys; print(sys.version)" 2>$null
        if ($v -match "3\.1[12]") { return $true }
    } catch {}
    return $false
}

function Install-Python312 {
    Write-Host "Python 3.12 not found — attempting install via winget..."
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Host "winget unavailable. Install Python 3.12 manually:" -ForegroundColor Yellow
        Write-Host "  https://www.python.org/downloads/ (check 'Add python.exe to PATH')"
        throw "Python 3.12 required"
    }
    & winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    if (-not (Test-Python312)) {
        throw "Python 3.12 install did not succeed. Install manually and re-run."
    }
}

function Copy-ProjectTree {
    param([string]$From, [string]$To)
    $exclude = @(".git", ".pytest_cache", "__pycache__", ".venv", "venv", "node_modules", ".env")
    if (-not (Test-Path $To)) {
        New-Item -ItemType Directory -Path $To -Force | Out-Null
    }
    Get-ChildItem -Path $From -Force | ForEach-Object {
        if ($exclude -contains $_.Name) { return }
        $dest = Join-Path $To $_.Name
        if ($_.PSIsContainer) {
            Copy-ProjectTree -From $_.FullName -To $dest
        } else {
            Copy-Item -Path $_.FullName -Destination $dest -Force
        }
    }
}

function Resolve-SourceRoot {
    if ($SourceRoot -and (Test-Path $SourceRoot)) {
        return (Resolve-Path $SourceRoot).Path
    }
    $parentGuess = Resolve-Path (Join-Path $ScriptDir "..\..")
    if (Test-Path (Join-Path $parentGuess "scalper\paper_runner.py")) {
        return $parentGuess.Path
    }
    if ($GitRepo) {
        $cloneTemp = Join-Path $env:TEMP "jarvis-one-clone-$(Get-Random)"
        Write-Host "Cloning $GitRepo ..."
        git clone --depth 1 $GitRepo $cloneTemp
        $sub = Join-Path $cloneTemp $ProjectName
        if (-not (Test-Path $sub)) {
            Remove-Item -Recurse -Force $cloneTemp -ErrorAction SilentlyContinue
            throw "Clone missing subfolder $ProjectName — use -SourceRoot instead."
        }
        return $sub
    }
    throw @"
No project source found.
  Copy the repo to the VPS (RDP drive redirect / zip), then run:
    -SourceRoot 'C:\Temp\futures-trend-day-l2-scalper'
  Or pass -GitRepo if the project is published to git.
"@
}

# --- 1. Python ---
Write-Host "`n[1/6] Checking Python..."
if (-not (Test-Python312)) {
    Install-Python312
} else {
    Write-Host "Python OK"
}

# --- 2. Live data paths (NT8 CSV) ---
Write-Host "`n[2/6] Creating live data directories..."
New-Item -ItemType Directory -Path $LiveDataDir -Force | Out-Null
New-Item -ItemType Directory -Path $ArchiveRoot -Force -ErrorAction SilentlyContinue | Out-Null
Write-Host "  NT8 bar CSV target: $BarCsvPath"
Write-Host "  Install ScalperL2Exporter in NT8 (integrations/ninjatrader8/) before starting paper_runner."

# --- 3. Copy project ---
Write-Host "`n[3/6] Installing project files..."
$resolvedSource = Resolve-SourceRoot
Write-Host "  Source: $resolvedSource"
Copy-ProjectTree -From $resolvedSource -To $InstallRoot

# --- 4. venv + pip ---
Write-Host "`n[4/6] Python venv and dependencies..."
$venvPath = Join-Path $InstallRoot ".venv"
$python = Join-Path $venvPath "Scripts\python.exe"
if (-not (Test-Path $python)) {
    py -3.12 -m venv $venvPath
    if (-not (Test-Path $python)) {
        python -m venv $venvPath
    }
}
& $python -m pip install --upgrade pip -q
& $python -m pip install -r (Join-Path $InstallRoot "requirements.txt") -q

# --- 5. .env (paper safety — no passwords) ---
Write-Host "`n[5/6] Writing .env (paper defaults)..."
$envFile = Join-Path $InstallRoot ".env"
$envContent = @"
# MergMoney VPS — paper trading only (generated by MergMoney_deploy.ps1)
# Live L2: NinjaTrader 8 ScalperL2Exporter → BAR_CSV_PATH (not futuresbot live recorder)
PAPER_ONLY=true
LIVE_TRADING=false

DATA_DIR=data
REPORTS_DIR=data/reports
LIVE_LOG_DIR=data/live
SCALPER_CONFIG=$ProductionConfig

BAR_CSV_PATH=$BarCsvPath
NT8_EXPORT_PATH=$Nt8ExportPath
FUTURESBOT_ARCHIVE_ROOT=$ArchiveRoot

RUNNER_MODE=follow
POLL_SECONDS=2
LOG_LEVEL=INFO
"@
Set-Content -Path $envFile -Value $envContent -Encoding UTF8
Write-Host "  Created: $envFile"

New-Item -ItemType Directory -Path (Join-Path $InstallRoot "data\live") -Force | Out-Null

# --- 6. Verify ---
Write-Host "`n[6/6] Verifying install..."
Set-Location $InstallRoot
& $python -c "import scalper; print('scalper import OK')"
$configPath = Join-Path $InstallRoot $ProductionConfig
if (-not (Test-Path $configPath)) {
    Write-Warning "Production config missing: $configPath"
} else {
    Write-Host "  Config: $ProductionConfig"
}

# --- Optional scheduled task ---
if ($RegisterScheduledTask) {
    $runScript = Join-Path $InstallRoot "deploy\windows\run_paper_bot.ps1"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-ExecutionPolicy Bypass -NoProfile -File `"$runScript`" -InstallRoot `"$InstallRoot`""
    $trigger = New-ScheduledTaskTrigger -Daily -At "09:25"
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
    Register-ScheduledTask -TaskName "MNQ-Paper-Scalper" -Action $action -Trigger $trigger `
        -Settings $settings -Description "Paper-only L2 scalper (follow mode)" -Force | Out-Null
    Write-Host "Registered Scheduled Task: MNQ-Paper-Scalper (09:25 daily)" -ForegroundColor Yellow
    Write-Host "Test manually first before relying on the task." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Deploy complete (PAPER ONLY) ===" -ForegroundColor Green
Write-Host "Install root: $InstallRoot"
Write-Host "Start paper bot:"
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$InstallRoot\deploy\windows\run_paper_bot.ps1`" -InstallRoot `"$InstallRoot`""
Write-Host ""
Write-Host "Ensure NinjaTrader 8 ScalperL2Exporter writes: $BarCsvPath"
Write-Host "  Copy integrations\ninjatrader8\ScalperL2Exporter.cs to Documents\NinjaTrader 8\bin\Custom\Strategies\"

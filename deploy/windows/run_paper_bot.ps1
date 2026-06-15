#Requires -Version 5.1
<#
.SYNOPSIS
  Run the scalper in paper/follow mode on Windows VPS.

.DESCRIPTION
  Loads .env, enforces PAPER_ONLY=true unless explicitly overridden.
  Never places live orders unless LIVE_TRADING=true AND LIVE_TRADING_CONFIRM=I_UNDERSTAND_RISK.
#>
param(
    [string]$InstallRoot = "C:\Bots\futures-trend-day-l2-scalper",
    [ValidateSet("follow", "replay")]
    [string]$Mode = "",
    [string]$DataPath = ""
)

$ErrorActionPreference = "Stop"
$envFile = Join-Path $InstallRoot ".env"

function Import-DotEnv {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        Write-Warning ".env not found at $Path — using defaults from .env.example"
        $Path = Join-Path $InstallRoot ".env.example"
    }
    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $eq = $line.IndexOf("=")
        if ($eq -lt 1) { return }
        $key = $line.Substring(0, $eq).Trim()
        $val = $line.Substring($eq + 1).Trim()
        $val = $val -replace '%USERPROFILE%', $env:USERPROFILE
        [Environment]::SetEnvironmentVariable($key, $val, "Process")
    }
}

Import-DotEnv -Path $envFile

# Hard safety: default to paper if unset
if (-not $env:PAPER_ONLY) { $env:PAPER_ONLY = "true" }
if (-not $env:LIVE_TRADING) { $env:LIVE_TRADING = "false" }

if ($env:PAPER_ONLY -eq "false" -and $env:LIVE_TRADING -eq "true") {
    if ($env:LIVE_TRADING_CONFIRM -ne "I_UNDERSTAND_RISK") {
        Write-Error "Live trading requested but LIVE_TRADING_CONFIRM=I_UNDERSTAND_RISK is not set. Aborting."
    }
    Write-Warning "LIVE TRADING FLAGS SET — gateway stub still does not send real orders."
} else {
    Write-Host "Paper mode active (PAPER_ONLY=$($env:PAPER_ONLY), LIVE_TRADING=$($env:LIVE_TRADING))" -ForegroundColor Green
}

$python = Join-Path $InstallRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "Python venv missing. Run deploy\windows\install.ps1 first."
}

$runnerMode = if ($Mode) { $Mode } else { $env:RUNNER_MODE }
if (-not $runnerMode) { $runnerMode = "follow" }

$barCsv = if ($DataPath) { $DataPath } else { $env:BAR_CSV_PATH }
if (-not $barCsv) { $barCsv = $env:NT8_EXPORT_PATH }
if (-not $barCsv) {
    Write-Error @"
BAR_CSV_PATH not set. Live L2 must come from NinjaTrader 8 ScalperL2Exporter.
Set BAR_CSV_PATH (or NT8_EXPORT_PATH) in .env, e.g.:
  C:\Bots\futures-trend-day-l2-scalper\data\live\nt8_mnq_1m.csv
See integrations\ninjatrader8\README.md
"@
}

if ($barCsv -match 'futuresbot\\live') {
    Write-Warning "BAR_CSV_PATH uses deprecated futuresbot live recorder. Use NT8 nt8_mnq_1m.csv instead."
}

$config = $env:SCALPER_CONFIG
if (-not $config) {
    $config = "configs/production/mnq_walkforward_optimized.yaml"
}
$configPath = Join-Path $InstallRoot $config

$logDir = $env:LIVE_LOG_DIR
if (-not $logDir) { $logDir = "data/live" }

Write-Host "Starting paper runner: mode=$runnerMode symbol-config=$configPath"
Write-Host "Bar CSV: $barCsv"
Write-Host "Logs:    $(Join-Path $InstallRoot $logDir)"

Set-Location $InstallRoot
& $python -m scalper.paper_runner `
    --config $configPath `
    --data $barCsv `
    --mode $runnerMode `
    --log-dir (Join-Path $InstallRoot $logDir) `
    --poll-seconds $($env:POLL_SECONDS)

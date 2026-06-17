#Requires -Version 5.1
<#
.SYNOPSIS
  Run the scalper in paper/follow mode on Windows VPS (single venv python instance).
#>
param(
    [string]$InstallRoot = "C:\Bots\futures-trend-day-l2-scalper",
    [ValidateSet("follow", "replay")]
    [string]$Mode = "",
    [string]$DataPath = ""
)

$ErrorActionPreference = "Stop"
$guardScript = Join-Path $PSScriptRoot "l2_scalper_instance_guard.ps1"
if (-not (Test-Path $guardScript)) {
    $guardScript = "C:\FuturesBot\scripts\l2_scalper_instance_guard.ps1"
}
if (-not (Test-Path $guardScript)) {
    Write-Error "l2_scalper_instance_guard.ps1 not found"
}
. $guardScript -InstallRoot $InstallRoot

$startupMutex = Enter-L2ScalperMutex -Name $script:L2ScalperStartupMutexName
if (-not $startupMutex) {
    Write-Host "SKIP: another L2 scalper startup in progress"
    exit 0
}

try {
    if (-not (Test-PaperRunnerHealthySingleton)) {
        Stop-L2ScalperPaperRunnerAll
    } elseif (Test-PaperRunnerHealthySingleton) {
        Write-Host "SKIP: paper_runner already running (venv singleton)"
        exit 0
    }

    Import-L2ScalperDotEnv -Path (Join-Path $InstallRoot ".env")
    if (-not $env:PAPER_ONLY) { $env:PAPER_ONLY = "true" }
    if (-not $env:LIVE_TRADING) { $env:LIVE_TRADING = "false" }

    if ($env:PAPER_ONLY -eq "false" -and $env:LIVE_TRADING -eq "true") {
        if ($env:LIVE_TRADING_CONFIRM -ne "I_UNDERSTAND_RISK") {
            Write-Error "Live trading requested but LIVE_TRADING_CONFIRM=I_UNDERSTAND_RISK is not set. Aborting."
        }
        Write-Warning "LIVE TRADING FLAGS SET - gateway stub still does not send real orders."
    } else {
        Write-Host "Paper mode active (PAPER_ONLY=$($env:PAPER_ONLY), LIVE_TRADING=$($env:LIVE_TRADING))" -ForegroundColor Green
    }

    $python = Get-L2ScalperVenvPython
    $runnerMode = if ($Mode) { $Mode } else { $env:RUNNER_MODE }
    if (-not $runnerMode) { $runnerMode = "follow" }

    $barCsv = if ($DataPath) { $DataPath } else { $env:BAR_CSV_PATH }
    if (-not $barCsv) { $barCsv = $env:NT8_EXPORT_PATH }
    if (-not $barCsv) {
        Write-Error "BAR_CSV_PATH not set. Set BAR_CSV_PATH in .env (NT8 ScalperL2Exporter CSV)."
    }

    $config = $env:SCALPER_CONFIG
    if (-not $config) { $config = "configs/production/mnq_walkforward_optimized.yaml" }
    $configPath = Join-Path $InstallRoot $config

    $logDir = $env:LIVE_LOG_DIR
    if (-not $logDir) { $logDir = "data/live" }

    Write-Host "Starting paper runner: mode=$runnerMode symbol-config=$configPath"
    Write-Host "Bar CSV: $barCsv"
    Write-Host "Python:  $python"

    Set-Location $InstallRoot
    & $python -m scalper.paper_runner `
        --config $configPath `
        --data $barCsv `
        --mode $runnerMode `
        --log-dir (Join-Path $InstallRoot $logDir) `
        --poll-seconds $($env:POLL_SECONDS)
} finally {
    try { $startupMutex.ReleaseMutex() } catch { }
    $startupMutex.Dispose()
}


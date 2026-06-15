#Requires -Version 5.1
<#
.SYNOPSIS
  Install futures-trend-day-l2-scalper on a Windows VPS (paper mode only).

.DESCRIPTION
  Creates venv, installs dependencies, copies .env.example if missing.
  Does NOT enable live trading. Does NOT store broker secrets.

.PARAMETER InstallRoot
  Target directory on the Windows VPS (default: C:\Bots\futures-trend-day-l2-scalper)

.PARAMETER SourceRoot
  Local path to the repo (default: parent of deploy\windows)
#>
param(
    [string]$InstallRoot = "C:\Bots\futures-trend-day-l2-scalper",
    [string]$SourceRoot = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $SourceRoot) {
    $SourceRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
}

Write-Host "=== futures-trend-day-l2-scalper Windows install ===" -ForegroundColor Cyan
Write-Host "Source: $SourceRoot"
Write-Host "Target: $InstallRoot"

function Copy-ProjectTree {
    param([string]$From, [string]$To)
    $exclude = @(
        ".git", ".pytest_cache", "__pycache__", ".venv", "venv",
        "node_modules", ".env"
    )
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

Write-Host "1. Copying project files..."
Copy-ProjectTree -From $SourceRoot -To $InstallRoot

$venvPath = Join-Path $InstallRoot ".venv"
$python = Join-Path $venvPath "Scripts\python.exe"

Write-Host "2. Creating Python venv..."
if (-not (Test-Path $python)) {
    py -3.12 -m venv $venvPath
    if (-not (Test-Path $python)) {
        python -m venv $venvPath
    }
}

Write-Host "3. Installing dependencies..."
& $python -m pip install --upgrade pip
& $python -m pip install -r (Join-Path $InstallRoot "requirements.txt")

$envExample = Join-Path $InstallRoot ".env.example"
$envFile = Join-Path $InstallRoot ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item $envExample $envFile
    Write-Host "Created .env from .env.example (paper defaults)." -ForegroundColor Yellow
} else {
    Write-Host ".env already exists — not overwritten." -ForegroundColor Yellow
}

$liveDir = Join-Path $InstallRoot "data\live"
New-Item -ItemType Directory -Path $liveDir -Force | Out-Null

$tradeLive = "C:\TradeData\futuresbot\live"
New-Item -ItemType Directory -Path $tradeLive -Force | Out-Null

Write-Host "4. Verifying install..."
& $python -c "import scalper; print('scalper ok')"
& $python -m pytest (Join-Path $InstallRoot "tests") -q --tb=no 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "pytest reported failures or missing tests — review manually." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Install complete (PAPER_ONLY=true by default) ===" -ForegroundColor Green
Write-Host "Next: edit $envFile (BAR_CSV_PATH, paths only — no secrets in git)"
Write-Host "Run:  powershell -File deploy\windows\run_paper_bot.ps1 -InstallRoot $InstallRoot"

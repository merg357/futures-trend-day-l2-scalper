#Requires -Version 5.1
<#
.SYNOPSIS
  Register (or remove) the MNQ paper scalper Windows Scheduled Task.

.DESCRIPTION
  Paper mode only. Run MergMoney_deploy.ps1 and validate manual start before using this.

.PARAMETER InstallRoot
  Bot install directory.

.PARAMETER Unregister
  Remove the scheduled task instead of creating it.
#>
param(
    [string]$InstallRoot = "C:\Bots\futures-trend-day-l2-scalper",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$TaskName = "MNQ-Paper-Scalper"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task: $TaskName"
    exit 0
}

$runScript = Join-Path $InstallRoot "deploy\windows\run_paper_bot.ps1"
if (-not (Test-Path $runScript)) {
    Write-Error "run_paper_bot.ps1 not found at $runScript — run MergMoney_deploy.ps1 first."
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -NoProfile -File `"$runScript`" -InstallRoot `"$InstallRoot`""
$trigger = New-ScheduledTaskTrigger -Daily -At "09:25"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Paper-only L2 scalper follow mode (PAPER_ONLY=true)" -Force

Write-Host "Registered: $TaskName — daily 09:25, runs run_paper_bot.ps1"
Write-Host "Manual test first: powershell -ExecutionPolicy Bypass -File `"$runScript`" -InstallRoot `"$InstallRoot`""

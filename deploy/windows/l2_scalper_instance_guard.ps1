# L2 scalper paper_runner single-instance guard (shared by start_l2_scalper.ps1 and run_paper_bot.ps1).
param(
    [string]$InstallRoot = "C:\Bots\futures-trend-day-l2-scalper"
)

$script:L2ScalperInstallRoot = $InstallRoot
$script:L2ScalperStateDir = Join-Path $InstallRoot "state"
$script:L2ScalperPidFile = Join-Path $script:L2ScalperStateDir "paper_runner.pid"
$script:L2ScalperStartupMutexName = "Global\FuturesBot.L2Scalper.PaperRunner.Startup"
$script:L2ScalperRunnerMutexName = "Global\FuturesBot.L2Scalper.PaperRunner"

function Ensure-L2ScalperStateDir {
    if (-not (Test-Path $script:L2ScalperStateDir)) {
        New-Item -ItemType Directory -Path $script:L2ScalperStateDir -Force | Out-Null
    }
}

function Get-L2ScalperVenvPython {
    $py = Join-Path $script:L2ScalperInstallRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $py)) {
        throw "Python venv missing at $py — run deploy\windows\install.ps1 first."
    }
    return (Resolve-Path -LiteralPath $py).Path
}

function Import-L2ScalperDotEnv {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        $Path = Join-Path $script:L2ScalperInstallRoot ".env.example"
    }
    if (-not (Test-Path $Path)) { return }
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

function Get-PaperRunnerPythonProcesses {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $cmd = $_.CommandLine
            if (-not $cmd) { return $false }
            ($cmd -match '-m\s+scalper\.paper_runner') -or ($cmd -match 'scalper\\paper_runner')
        }
}

function Get-PaperRunnerRootProcesses {
    $all = @(Get-PaperRunnerPythonProcesses)
    if ($all.Count -eq 0) { return @() }
    $byPid = @{}
    foreach ($p in $all) { $byPid[$p.ProcessId] = $p }
    $roots = foreach ($p in $all) {
        $parent = $p.ParentProcessId
        $parentProc = $byPid[$parent]
        if ($parentProc -and ($parentProc.CommandLine -match 'scalper\.paper_runner|paper_runner')) { continue }
        $p
    }
    return @($roots)
}

function Get-PaperRunnerWrapperProcesses {
    Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $cmd = $_.CommandLine
            if (-not $cmd) { return $false }
            $cmd -match 'run_paper_bot\.ps1' -or ($cmd -match 'start_l2_scalper\.ps1')
        }
}

function Test-PaperRunnerPidAlive {
    param([string]$ExpectedPython = "")
    Ensure-L2ScalperStateDir
    if (-not (Test-Path $script:L2ScalperPidFile)) { return $false }
    $raw = (Get-Content $script:L2ScalperPidFile -Raw -ErrorAction SilentlyContinue).Trim()
    if ($raw -notmatch '^\d+$') { return $false }
    $pidNum = [int]$raw
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $pidNum" -ErrorAction SilentlyContinue
    if (-not $proc -or $proc.Name -ne 'python.exe') { return $false }
    $cmd = $proc.CommandLine
    if ($cmd -notmatch 'scalper\.paper_runner') { return $false }
    if ($ExpectedPython -and $proc.ExecutablePath -and ($proc.ExecutablePath -ne $ExpectedPython)) {
        # Windows venv child may own the pid file while root is venv shim.
        $roots = @(Get-PaperRunnerRootProcesses)
        if ($roots.Count -ne 1) { return $false }
        if ($roots[0].ExecutablePath -ne $ExpectedPython) { return $false }
        return $true
    }
    if ($proc.ExecutablePath -and ($proc.ExecutablePath -notmatch '\\\.venv\\')) {
        $roots = @(Get-PaperRunnerRootProcesses)
        return ($roots.Count -eq 1 -and $roots[0].ExecutablePath -match '\\\.venv\\')
    }
    return $true
}

function Stop-L2ScalperPaperRunnerAll {
    param([int]$KeepPid = 0)
    foreach ($p in @(Get-PaperRunnerRootProcesses)) {
        if ($KeepPid -gt 0 -and $p.ProcessId -eq $KeepPid) { continue }
        Write-Host "Stopping paper_runner root PID=$($p.ProcessId) exe=$($p.ExecutablePath)"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    foreach ($p in @(Get-PaperRunnerPythonProcesses)) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    foreach ($w in @(Get-PaperRunnerWrapperProcesses)) {
        Write-Host "Stopping paper_runner wrapper PID=$($w.ProcessId)"
        Stop-Process -Id $w.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path $script:L2ScalperPidFile) {
        Remove-Item $script:L2ScalperPidFile -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 800
}

function Enter-L2ScalperMutex {
    param([string]$Name)
    $createdNew = $false
    $mutex = New-Object System.Threading.Mutex($false, $Name, [ref]$createdNew)
    try {
        if (-not $mutex.WaitOne(3000, $false)) {
            $mutex.Dispose()
            return $null
        }
    } catch [System.Threading.AbandonedMutexException] {
        # Previous starter crashed without releasing; we own the mutex now.
    }
    return $mutex
}

function Test-PaperRunnerHealthySingleton {
    $venvPy = Get-L2ScalperVenvPython
    $roots = @(Get-PaperRunnerRootProcesses)
    if ($roots.Count -eq 0) {
        return (Test-PaperRunnerPidAlive -ExpectedPython $venvPy)
    }
    if ($roots.Count -gt 1) { return $false }
    $p = $roots[0]
    if ($p.ExecutablePath -and ($p.ExecutablePath -ne $venvPy)) { return $false }
    return $true
}

function Get-L2ScalperPaperRunnerArgumentList {
    param([string]$InstallRoot)
    $script:L2ScalperInstallRoot = $InstallRoot
    Import-L2ScalperDotEnv -Path (Join-Path $InstallRoot ".env")
    if (-not $env:PAPER_ONLY) { $env:PAPER_ONLY = "true" }
    if (-not $env:LIVE_TRADING) { $env:LIVE_TRADING = "false" }
    $runnerMode = if ($env:RUNNER_MODE) { $env:RUNNER_MODE } else { "follow" }
    $barCsv = $env:BAR_CSV_PATH
    if (-not $barCsv) { $barCsv = $env:NT8_EXPORT_PATH }
    if (-not $barCsv) { $barCsv = Join-Path $InstallRoot "data\live\nt8_mnq_1m.csv" }
    $config = $env:SCALPER_CONFIG
    if (-not $config) { $config = "configs/production/mnq_walkforward_optimized.yaml" }
    $configPath = Join-Path $InstallRoot $config
    $logDir = $env:LIVE_LOG_DIR
    if (-not $logDir) { $logDir = "data/live" }
    $poll = $env:POLL_SECONDS
    if (-not $poll) { $poll = "2" }
    return @(
        "-m", "scalper.paper_runner",
        "--config", $configPath,
        "--data", $barCsv,
        "--mode", $runnerMode,
        "--log-dir", (Join-Path $InstallRoot $logDir),
        "--poll-seconds", $poll
    )
}

function Start-L2ScalperPaperRunnerDetached {
    param([string]$InstallRoot)
    $script:L2ScalperInstallRoot = $InstallRoot
    $python = Get-L2ScalperVenvPython
    $args = Get-L2ScalperPaperRunnerArgumentList -InstallRoot $InstallRoot
    Start-Process -FilePath $python -ArgumentList $args -WorkingDirectory $InstallRoot -WindowStyle Hidden | Out-Null
}



# Launch MES/ES/NQ runner via FuturesBot ops script (VPS standard path).
param([switch]$Force)
$fb = "C:\FuturesBot\scripts\start_mes_es_nq_runner.ps1"
if (-not (Test-Path $fb)) { Write-Error "Missing $fb"; exit 1 }
if ($Force) { & $fb -Force } else { & $fb }

# Stop A4 live runner. Kills PowerShell wrapper + venv launcher + Python interpreter.
# Usage: .\stop_runner.ps1
# Comments use ASCII only to avoid PowerShell 5.1 GBK/UTF-8 parsing issues.

chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

Write-Host "=== Stop A4 Runner ===" -ForegroundColor Yellow

$procs = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match 'live_signal_runner|start_runner'
})

if ($procs.Count -eq 0) {
    Write-Host "No running runner processes." -ForegroundColor Green
    if (Test-Path "user_data\notebooks\live_signal_runner.pid") {
        Remove-Item "user_data\notebooks\live_signal_runner.pid" -Force
        Write-Host "Removed stale PID file." -ForegroundColor Gray
    }
    exit 0
}

Write-Host "Found $($procs.Count) related process(es):" -ForegroundColor Cyan
$procs | ForEach-Object {
    $cmdShort = if ($_.CommandLine.Length -gt 100) {
        $_.CommandLine.Substring(0, 100) + "..."
    } else {
        $_.CommandLine
    }
    Write-Host ("  PID {0,-6}  {1}" -f $_.ProcessId, $cmdShort)
}

Write-Host ""
Write-Host "Terminating..." -ForegroundColor Yellow
$killed = 0
foreach ($p in $procs) {
    try {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
        Write-Host "  [OK] killed PID $($p.ProcessId)" -ForegroundColor Green
        $killed++
    } catch {
        Write-Host "  [FAIL] PID $($p.ProcessId): $_" -ForegroundColor Red
    }
}

Start-Sleep -Seconds 2

$remaining = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match 'live_signal_runner|start_runner'
})

if (Test-Path "user_data\notebooks\live_signal_runner.pid") {
    Remove-Item "user_data\notebooks\live_signal_runner.pid" -Force
    Write-Host "[OK] Cleaned PID file" -ForegroundColor Green
}

Write-Host ""
if ($remaining.Count -eq 0) {
    Write-Host "=== Done ($killed processes killed) ===" -ForegroundColor Green
} else {
    Write-Host "=== WARNING: $($remaining.Count) process(es) still alive ===" -ForegroundColor Red
    $remaining | Select-Object ProcessId, CommandLine | Format-List
    Write-Host "Manual: Stop-Process -Id <PID> -Force" -ForegroundColor Yellow
}

# Start A4 live runner with proper UTF-8 handling for console + log file.
# Usage: .\start_runner.ps1
# Comments use ASCII only to avoid PowerShell 5.1 parsing issues with mixed encodings.

chcp 65001 | Out-Null

# Make Python, console, PS pipeline all use UTF-8
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::InputEncoding  = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()

$logPath = "logs\runner_a4.log"
if (-not (Test-Path "logs")) { New-Item -ItemType Directory logs | Out-Null }

Write-Host "=== A4 Runner starting (UTF-8) ===" -ForegroundColor Cyan
Write-Host "Log file: $logPath"
Write-Host ""

# Stream Python stdout to console AND append to UTF-8 log file
.venv\Scripts\python -u user_data/notebooks/live_signal_runner.py 2>&1 |
    ForEach-Object {
        Write-Host $_
        Add-Content -Path $logPath -Value $_ -Encoding utf8
    }

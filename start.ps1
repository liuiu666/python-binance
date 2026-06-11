# BTC 10m Binary Options Controller & Deployer
Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "   BTC 10m Binary Options System Controller" -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Cyan

function Get-PortPids {
    param([int]$Port)
    $lines = netstat -ano | Select-String -Pattern ":$Port\s+"
    $pids = @()
    foreach ($line in $lines) {
        $parts = ($line.ToString().Trim() -split "\s+")
        if ($parts.Length -ge 5 -and $parts[1] -match ":$Port$" -and $parts[3] -eq "LISTENING") {
            $pids += [int]$parts[4]
        }
    }
    return $pids | Select-Object -Unique
}

function Stop-PortProcess {
    param(
        [int]$Port,
        [string]$Label
    )
    $pids = Get-PortPids -Port $Port
    foreach ($procId in $pids) {
        if ($procId -gt 0) {
            Write-Host "  -> Terminating $Label process (PID $procId)..." -ForegroundColor Gray
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        }
    }
    if ($pids.Count -gt 0) {
        Start-Sleep -Seconds 1
    }
}

Write-Host "[1/5] Cleaning Node server port 3000..." -ForegroundColor Yellow
Stop-PortProcess -Port 3000 -Label "conflicting Node"

Write-Host "[2/5] Cleaning price proxy port 39870..." -ForegroundColor Yellow
Stop-PortProcess -Port 39870 -Label "conflicting Python PriceProxy"

Write-Host "[3/5] Cleaning signal service port 39871..." -ForegroundColor Yellow
Stop-PortProcess -Port 39871 -Label "conflicting Python Signal"
Write-Host "  -> All ports are clean and ready." -ForegroundColor Green

Write-Host "[4/5] Bundling latest React frontend with Vite..." -ForegroundColor Yellow
npm run frontend:build
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Vite compilation failed. Please check frontend/src for errors."
    exit $LASTEXITCODE
}
Write-Host "  -> Frontend compiled successfully." -ForegroundColor Green

Write-Host "[5/5] Launching background services..." -ForegroundColor Yellow
$env:SERVER_SIM_TRADING_ENABLED = "1"
$env:APP_DIR = (Get-Location).Path
$env:DATA_DIR = (Join-Path (Get-Location).Path "data")
$env:ENABLE_SIGNAL_SHADOWS = "0"
$env:ENABLE_LEGACY_TWO_MINUTE_LIVE = "0"

Write-Host "  -> Launching Python Price Feeder (py/price_proxy.py)..." -ForegroundColor Gray
$proxyProcess = Start-Process -FilePath "python" `
    -ArgumentList "py/price_proxy.py" `
    -RedirectStandardOutput ".price.out" `
    -RedirectStandardError ".price.err" `
    -WindowStyle Hidden `
    -PassThru

Write-Host "  -> Launching Node.js Server (server.js)..." -ForegroundColor Gray
$process = Start-Process -FilePath "node" `
    -ArgumentList "server.js" `
    -RedirectStandardOutput ".srv.out" `
    -RedirectStandardError ".srv.err" `
    -WindowStyle Hidden `
    -PassThru

Start-Sleep -Seconds 3

$newPids = Get-PortPids -Port 3000
if ($newPids.Count -gt 0) {
    Write-Host "`n====================================================" -ForegroundColor Green
    Write-Host " BTC 10m Binary Options System Started Successfully!" -ForegroundColor Green
    Write-Host "====================================================" -ForegroundColor Green
    Write-Host "  * URL Address   : http://localhost:3000" -ForegroundColor Cyan
    Write-Host "  * Strategies    : BTC_10min_SAFE + BTC_10min_TAKER" -ForegroundColor Cyan
    Write-Host "  * Research Mode : Disabled" -ForegroundColor Cyan
    Write-Host "  * Login         : sl / sl,123321" -ForegroundColor Cyan
    Write-Host "====================================================`n" -ForegroundColor Green
} else {
    Write-Warning "Server failed to bind to port 3000."
}

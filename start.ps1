# BTC 10m/30m Binary Options Controller & Deployer
Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

Write-Host "====================================================" -ForegroundColor Cyan
Write-Host "   BTC Binary Options System Controller & Deployer" -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Cyan

# 1. Kill any process holding port 3000 (Node Server)
Write-Host "[1/4] Scanning and cleaning port 3000..." -ForegroundColor Yellow
$conn = Get-NetTCPConnection -LocalPort 3000 -ErrorAction SilentlyContinue
if ($conn) {
    $pids = $conn.OwningProcess | Select-Object -Unique
    foreach ($procId in $pids) {
        if ($procId -gt 0) {
            Write-Host "  -> Terminating conflicting Node process (PID $procId)..." -ForegroundColor Gray
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Seconds 1
}

# 2. Kill any process holding port 39870 (Python Price Proxy)
Write-Host "[2/4] Scanning and cleaning price proxy port 39870..." -ForegroundColor Yellow
$proxyConn = Get-NetTCPConnection -LocalPort 39870 -ErrorAction SilentlyContinue
if ($proxyConn) {
    $proxyPids = $proxyConn.OwningProcess | Select-Object -Unique
    foreach ($procId in $proxyPids) {
        if ($procId -gt 0) {
            Write-Host "  -> Terminating conflicting Python PriceProxy process (PID $procId)..." -ForegroundColor Gray
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Seconds 1
}
Write-Host "  -> All ports are clean and ready." -ForegroundColor Green

# 3. Recompile frontend assets to ensure latest build is active
Write-Host "[3/4] Bundling latest React frontend with Vite..." -ForegroundColor Yellow
npm run frontend:build
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Vite compilation failed. Please check frontend/src/App.jsx for errors."
    exit $LASTEXITCODE
}
Write-Host "  -> Frontend compiled successfully." -ForegroundColor Green

# 4. Start services in the background
Write-Host "[4/4] Launching background services..." -ForegroundColor Yellow
$env:SERVER_SIM_TRADING_ENABLED="1"

# Launch Python Price Proxy as a detached persistent OS process
Write-Host "  -> Launching Python Price Feeder (py/price_proxy.py)..." -ForegroundColor Gray
$proxyProcess = Start-Process -FilePath "python" -ArgumentList "py/price_proxy.py" -NoNewWindow -PassThru

# Launch Node server as a detached persistent OS process
Write-Host "  -> Launching Node.js Server (server.js)..." -ForegroundColor Gray
$process = Start-Process -FilePath "node" -ArgumentList "server.js" -NoNewWindow -PassThru

# Wait a few seconds to let them bind
Start-Sleep -Seconds 3

# Verify if port 3000 is active now
$newConn = Get-NetTCPConnection -LocalPort 3000 -ErrorAction SilentlyContinue
if ($newConn) {
    Write-Host "`n====================================================" -ForegroundColor Green
    Write-Host " 🎉 BTC Binary Options System Started Successfully!" -ForegroundColor Green
    Write-Host "====================================================" -ForegroundColor Green
    Write-Host "  * URL Address   : http://localhost:3000" -ForegroundColor Cyan
    Write-Host "  * Shadow Trade  : Active (Background Mock Trading)" -ForegroundColor Cyan
    Write-Host "  * Accounts & Passwords:" -ForegroundColor Cyan
    Write-Host "    -> User: sl  | Password: sl,123321" -ForegroundColor Cyan
    Write-Host "    -> User: lsl | Password: 123321" -ForegroundColor Cyan
    Write-Host "====================================================`n" -ForegroundColor Green
    Write-Host "Note: To stop all services later, run start.ps1 again to auto-clean ports!" -ForegroundColor Gray
} else {
    Write-Warning "Server failed to bind to port 3000."
}

param(
    [ValidateSet("start", "stop", "restart", "status", "reload-signal", "pause-auto", "resume-auto", "refresh-data", "refresh-reports", "help")]
    [string]$Action = "status",
    [int]$Port = 3000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerScript = Join-Path $RepoRoot "server.js"
$SignalFile = Join-Path $RepoRoot "data\live_signals.json"
$TradeConfigFile = Join-Path $RepoRoot "data\trade_config.json"
$DataUpdateStatusFile = Join-Path $RepoRoot "data\live_data_update_status.json"
$TradeAuditFile = Join-Path $RepoRoot "data\trade_audit.jsonl"
$ServerStdout = Join-Path $RepoRoot ".srv.out"
$ServerStderr = Join-Path $RepoRoot ".srv.err"
$ServerUrl = "http://127.0.0.1:$Port"

function Write-Info {
    param([string]$Message)
    Write-Host "[service] $Message"
}

function Get-ServerProcess {
    try {
        $pattern = "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$"
        $line = netstat -ano -p tcp | Select-String $pattern | Select-Object -First 1
        if ($line) {
            $pid = [int]$line.Matches[0].Groups[1].Value
            $proc = Get-Process -Id $pid -ErrorAction Stop
            return [pscustomobject]@{
                ProcessId = $proc.Id
                CreationDate = $proc.StartTime
                CommandLine = "node server.js"
            }
        }
    } catch {
    }

    $items = Get-ProcessSnapshot |
        Where-Object { $_.CommandLine -like "*server.js*" } |
        Sort-Object CreationDate -Descending
    if ($items) { return $items[0] }
    return $null
}

function Get-SignalProcesses {
    @(Get-ProcessSnapshot |
        Where-Object { $_.CommandLine -like "*signal_btc.py*" } |
        Sort-Object CreationDate)
}

function Get-NodePath {
    return (Get-Command node -ErrorAction Stop).Source
}

function Get-PreferredBaseUrl {
    $addresses = @()
    try {
        $all = [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()
        foreach ($nic in $all) {
            if ($nic.OperationalStatus -ne [System.Net.NetworkInformation.OperationalStatus]::Up) { continue }
            foreach ($uni in $nic.GetIPProperties().UnicastAddresses) {
                if ($uni.Address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) { continue }
                $ip = $uni.Address.ToString()
                if ($ip -eq "127.0.0.1") { continue }
                $addresses += $ip
            }
        }
    } catch {
    }

    $preferred = $addresses | Where-Object { $_ -like "192.168.*" } | Select-Object -First 1
    if (-not $preferred) {
        $preferred = $addresses | Where-Object { $_ -like "10.*" -or $_ -like "172.*" } | Select-Object -First 1
    }
    if (-not $preferred) {
        $preferred = $addresses | Select-Object -First 1
    }
    if ($preferred) {
        return "http://${preferred}:$Port"
    }
    return $ServerUrl
}

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $null }
    return Get-Content $Path -Raw | ConvertFrom-Json
}

function Get-ProcessSnapshot {
    $existing = Get-Variable -Scope Script -Name ProcessSnapshot -ErrorAction SilentlyContinue
    if ($existing -and $existing.Value) { return $existing.Value }
    $script:ProcessSnapshot = @(Get-CimInstance Win32_Process -Filter "name = 'node.exe' or name = 'python.exe'")
    return $script:ProcessSnapshot
}

function Clear-ProcessSnapshot {
    $script:ProcessSnapshot = $null
}

function Get-ObjectValue {
    param(
        $InputObject,
        [string]$Name,
        $Default = $null
    )

    if ($null -eq $InputObject) { return $Default }
    $prop = $InputObject.PSObject.Properties[$Name]
    if ($null -ne $prop) { return $prop.Value }
    return $Default
}

function Format-DisplayValue {
    param(
        $Value,
        [string]$Default = "<none>"
    )

    if ($null -eq $Value) { return $Default }
    $text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($text)) { return $Default }
    return $text
}

function Read-JsonlTail {
    param(
        [string]$Path,
        [int]$Tail = 120
    )

    if (-not (Test-Path $Path)) { return @() }
    $lines = Get-Content $Path -Tail $Tail
    $items = @()
    foreach ($line in $lines) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        try {
            $items += ($line | ConvertFrom-Json)
        } catch {
        }
    }
    return $items
}

function Get-TabletSummary {
    $rows = Read-JsonlTail -Path $TradeAuditFile -Tail 200
    $latestHeartbeat = $null
    $latestOrderDone = $null

    foreach ($row in ($rows | Sort-Object serverTime)) {
        if ($row.event -eq "autojs_heartbeat") { $latestHeartbeat = $row }
        if ($row.event -eq "order_done") { $latestOrderDone = $row }
    }

    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $heartbeatAgeMs = $null
    if ($latestHeartbeat -and $latestHeartbeat.serverTime) {
        $heartbeatAgeMs = [int64]$now - [int64]$latestHeartbeat.serverTime
    }

    $status = "waiting_for_autojs_events"
    if ($latestOrderDone) {
        $status = "has_order_done"
    } elseif ($heartbeatAgeMs -ne $null -and $heartbeatAgeMs -le 120000) {
        $status = "autojs_online_waiting_for_order_done"
    } elseif ($latestHeartbeat) {
        $status = "autojs_seen_waiting_for_order_done"
    }

    return [pscustomobject]@{
        status = $status
        latestHeartbeatAgeMs = $heartbeatAgeMs
        latestHeartbeat = $latestHeartbeat
        latestOrderDone = $latestOrderDone
    }
}

function Invoke-LocalApi {
    param(
        [string]$Path,
        [string]$Method = "Get",
        $Body = $null
    )

    $uri = "$ServerUrl$Path"
    if ($Method -eq "Get") {
        return Invoke-RestMethod -Method Get -Uri $uri
    }

    $json = if ($null -eq $Body) { $null } else { $Body | ConvertTo-Json -Depth 10 -Compress }
    return Invoke-RestMethod -Method $Method -Uri $uri -ContentType "application/json" -Body $json
}

function Test-ApiReady {
    try {
        $null = Invoke-LocalApi -Path "/api/runtime"
        return $true
    } catch {
        return $false
    }
}

function Wait-ForApi {
    param(
        [int]$TimeoutSec = 30
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-ApiReady) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Wait-ForSignalService {
    param(
        [int]$TimeoutSec = 30
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $svc = Invoke-LocalApi -Path "/api/signal-service"
            if ($svc.running -and $svc.pid) { return $svc }
        } catch {
        }
        Start-Sleep -Seconds 1
    }
    return $null
}

function Stop-ProcessesById {
    param([int[]]$Ids)
    foreach ($id in $Ids) {
        if (-not $id) { continue }
        try {
            Stop-Process -Id $id -ErrorAction Stop
        } catch {
            try {
                Stop-Process -Id $id -Force -ErrorAction Stop
            } catch {
            }
        }
    }
}

function Get-PrimarySignal {
    $signalData = Read-JsonFile -Path $SignalFile
    if ($null -eq $signalData) { return $null }
    if ($signalData.PSObject.Properties.Name -contains "BTC_10min") {
        return $signalData.BTC_10min
    }
    foreach ($prop in $signalData.PSObject.Properties) {
        return $prop.Value
    }
    return $null
}

function Show-Status {
    $server = Get-ServerProcess
    $config = Read-JsonFile -Path $TradeConfigFile
    $dataUpdateStatus = Read-JsonFile -Path $DataUpdateStatusFile
    $tablet = Get-TabletSummary
    $baseUrl = Get-PreferredBaseUrl
    $signalSvc = $null

    if ($server) {
        try { $signalSvc = Invoke-LocalApi -Path "/api/signal-service" } catch {}
    }

    $signal = Get-PrimarySignal

    Write-Host ""
    Write-Host "Service status"
    Write-Host "--------------"
    if ($server) {
        Write-Host ("Server      : running  pid={0}  started={1}" -f $server.ProcessId, ([datetime]$server.CreationDate))
    } else {
        Write-Host "Server      : stopped"
    }

    if ($signalSvc -and $signalSvc.running) {
        Write-Host ("Signal      : running  pid={0}" -f $signalSvc.pid)
    } else {
        Write-Host "Signal      : stopped"
    }

    Write-Host ("URL         : {0}" -f $baseUrl)
    Write-Host ("Loader      : {0}/auto_btc_loader.js" -f $baseUrl)

    if ($config) {
        Write-Host ("AutoTrade   : {0}  amount={1}  minConfidence={2}" -f $config.autoTrade, $config.amount, $config.minConfidence)
    } else {
        Write-Host "AutoTrade   : unavailable"
    }

    if ($signal) {
        $policyName = Format-DisplayValue (Get-ObjectValue -InputObject $signal -Name "policy_name")
        $regimeGroup = Format-DisplayValue (Get-ObjectValue -InputObject $signal -Name "regime_group")
        $signalName = Format-DisplayValue (Get-ObjectValue -InputObject $signal -Name "signal")
        $actionableTime = Format-DisplayValue (Get-ObjectValue -InputObject $signal -Name "actionable_time")
        Write-Host ("Policy      : {0}" -f $policyName)
        Write-Host ("Signal      : {0}  regime={1}  actionable={2}" -f $signalName, $regimeGroup, $actionableTime)
    } else {
        Write-Host "Policy      : unavailable"
        Write-Host "Signal      : unavailable"
    }

    $signalBlocked = Get-ObjectValue -InputObject $signal -Name "data_health_blocked"
    if ($signalBlocked -eq $true) {
        Write-Host "Data health : blocked"
    } elseif ($dataUpdateStatus -and (Get-ObjectValue -InputObject $dataUpdateStatus -Name "ok") -eq $true) {
        Write-Host "Data health : ok"
    } elseif ($dataUpdateStatus -and (Get-ObjectValue -InputObject $dataUpdateStatus -Name "ok") -eq $false) {
        Write-Host "Data health : failed"
    } else {
        Write-Host "Data health : unknown"
    }

    if ($tablet) {
        $tabletStatus = Get-ObjectValue -InputObject $tablet -Name "status" -Default "unknown"
        $heartbeatAge = Get-ObjectValue -InputObject $tablet -Name "latestHeartbeatAgeMs"
        Write-Host ("Tablet      : {0}" -f $tabletStatus)
        if ($heartbeatAge -ne $null) {
            Write-Host ("Heartbeat   : age_ms={0}" -f $heartbeatAge)
        }
    } else {
        Write-Host "Tablet      : unavailable"
    }

    if ($signalSvc) {
        Write-Host ("Signal svc  : running={0} pid={1}" -f $signalSvc.running, $signalSvc.pid)
    } else {
        Write-Host "Signal svc  : unavailable"
    }

    Write-Host ""
}

function Start-ServiceStack {
    $server = Get-ServerProcess
    if ($server) {
        Write-Info ("server already running (pid={0})" -f $server.ProcessId)
        Show-Status
        return
    }

    $node = Get-NodePath
    Write-Info ("starting server.js with {0}" -f $node)
    $proc = Start-Process -FilePath $node `
        -ArgumentList "server.js" `
        -WorkingDirectory $RepoRoot `
        -RedirectStandardOutput $ServerStdout `
        -RedirectStandardError $ServerStderr `
        -WindowStyle Hidden `
        -PassThru

    if (-not (Wait-ForApi -TimeoutSec 30)) {
        throw "server started as pid=$($proc.Id), but API did not become ready within 30s"
    }

    $svc = Wait-ForSignalService -TimeoutSec 30
    if ($null -eq $svc) {
        throw "server API is up, but signal service did not report running within 30s"
    }

    Clear-ProcessSnapshot
    Write-Info ("server started (pid={0})" -f $proc.Id)
    Show-Status
}

function Stop-ServiceStack {
    $server = Get-ServerProcess
    $signalProcs = Get-SignalProcesses

    if (-not $server -and @($signalProcs).Count -eq 0) {
        Write-Info "nothing is running"
        return
    }

    if ($server) {
        Write-Info ("stopping server pid={0}" -f $server.ProcessId)
        Stop-ProcessesById -Ids @([int]$server.ProcessId)
        Start-Sleep -Seconds 2
        Clear-ProcessSnapshot
    }

    $remainingSignals = Get-SignalProcesses
    if (@($remainingSignals).Count -gt 0) {
        $ids = @($remainingSignals | ForEach-Object { [int]$_.ProcessId })
        Write-Info ("stopping remaining signal processes: {0}" -f ($ids -join ","))
        Stop-ProcessesById -Ids $ids
        Clear-ProcessSnapshot
    }

    Start-Sleep -Seconds 1
    Show-Status
}

function Restart-ServiceStack {
    Stop-ServiceStack
    Start-Sleep -Seconds 2
    Start-ServiceStack
}

function Reload-SignalService {
    $server = Get-ServerProcess
    if (-not $server) {
        throw "server.js is not running"
    }

    $before = $null
    try { $before = Invoke-LocalApi -Path "/api/signal-service" } catch {}
    $signalProcs = Get-SignalProcesses
    if (@($signalProcs).Count -eq 0) {
        Write-Info "signal process is not running, waiting for auto-restart"
    } else {
        $ids = @($signalProcs | ForEach-Object { [int]$_.ProcessId })
        Write-Info ("reloading signal service, stopping pids={0}" -f ($ids -join ","))
        Stop-ProcessesById -Ids $ids
        Clear-ProcessSnapshot
    }

    $deadline = (Get-Date).AddSeconds(35)
    do {
        Start-Sleep -Seconds 1
        try {
            $current = Invoke-LocalApi -Path "/api/signal-service"
            if ($current.running -and $current.pid -and (($null -eq $before) -or ($current.pid -ne $before.pid))) {
                Write-Info ("signal service restarted (pid={0})" -f $current.pid)
                Show-Status
                return
            }
        } catch {
        }
    } while ((Get-Date) -lt $deadline)

    throw "signal service did not restart within 35s"
}

function Set-AutoTrade {
    param([bool]$Enabled)
    if (-not (Test-ApiReady)) {
        throw "server API is not ready"
    }

    $resp = Invoke-LocalApi -Path "/api/config" -Method "Post" -Body @{ autoTrade = $Enabled }
    if ($Enabled -and $resp.safetyBlocked) {
        Write-Info "resume request was blocked by safety gate"
    } else {
        Write-Info ("autoTrade set to {0}" -f $resp.autoTrade)
    }
    Show-Status
}

function Refresh-Data {
    if (-not (Test-ApiReady)) {
        throw "server API is not ready"
    }
    $resp = Invoke-LocalApi -Path "/api/data-update/refresh" -Method "Post"
    Write-Info ("data refresh requested: ok={0}" -f $resp.ok)
}

function Refresh-Reports {
    if (-not (Test-ApiReady)) {
        throw "server API is not ready"
    }
    $resp = Invoke-LocalApi -Path "/api/reports/refresh" -Method "Post"
    Write-Info ("report refresh requested: ok={0}" -f $resp.ok)
}

function Show-Help {
    Write-Host @"
Usage:
  .\service.ps1 start
  .\service.ps1 stop
  .\service.ps1 restart
  .\service.ps1 status
  .\service.ps1 reload-signal
  .\service.ps1 pause-auto
  .\service.ps1 resume-auto
  .\service.ps1 refresh-data
  .\service.ps1 refresh-reports

Examples:
  .\service.ps1 start
  .\service.ps1 status
  .\service.ps1 pause-auto
"@
}

switch ($Action) {
    "start"           { Start-ServiceStack; break }
    "stop"            { Stop-ServiceStack; break }
    "restart"         { Restart-ServiceStack; break }
    "status"          { Show-Status; break }
    "reload-signal"   { Reload-SignalService; break }
    "pause-auto"      { Set-AutoTrade -Enabled:$false; break }
    "resume-auto"     { Set-AutoTrade -Enabled:$true; break }
    "refresh-data"    { Refresh-Data; break }
    "refresh-reports" { Refresh-Reports; break }
    "help"            { Show-Help; break }
    default           { Show-Help; break }
}

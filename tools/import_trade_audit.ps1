param(
    [string]$ServerUrl = "http://115.190.218.128:3000",
    [string]$AuditPath = "",
    [string]$Token = $env:API_TOKEN,
    [int]$BatchSize = 200,
    [int]$Limit = 0,
    [string]$Source = "local_import"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $AuditPath) {
    $AuditPath = Join-Path $RepoRoot "data\trade_audit.jsonl"
}
if (-not (Test-Path $AuditPath)) {
    throw "Audit file not found: $AuditPath"
}

$ServerUrl = $ServerUrl.TrimEnd("/")
$uri = "$ServerUrl/api/trade-audit/import"
$headers = @{}
if ($Token) {
    $headers["X-API-Token"] = $Token
}

$lines = Get-Content -Path $AuditPath
if ($Limit -gt 0 -and $lines.Count -gt $Limit) {
    $lines = $lines | Select-Object -Last $Limit
}

$items = @()
foreach ($line in $lines) {
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    try {
        $items += ($line | ConvertFrom-Json)
    } catch {
        Write-Warning "Skip invalid JSONL row"
    }
}

if (-not $items.Count) {
    Write-Host "No audit rows to import."
    exit 0
}

$totalImported = 0
$totalSkipped = 0
for ($i = 0; $i -lt $items.Count; $i += $BatchSize) {
    $batch = @($items[$i..([Math]::Min($i + $BatchSize - 1, $items.Count - 1))])
    $body = @{
        source = $Source
        items = $batch
    } | ConvertTo-Json -Depth 50 -Compress
    $res = Invoke-RestMethod -Method Post -Uri $uri -ContentType "application/json" -Headers $headers -Body $body
    $totalImported += [int]$res.imported
    $totalSkipped += [int]$res.skipped
    Write-Host ("Batch {0}-{1}: imported={2} skipped={3}" -f ($i + 1), ($i + $batch.Count), $res.imported, $res.skipped)
}

Write-Host ("Done. rows={0} imported={1} skipped={2} server={3}" -f $items.Count, $totalImported, $totalSkipped, $ServerUrl)

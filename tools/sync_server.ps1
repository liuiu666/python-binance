param(
    [string]$ServerHost = "115.190.218.128",
    [string]$ServerUser = "root",
    [string]$RemotePath = "/opt/codex",
    [string]$RestartCommand = "",
    [switch]$SkipTests,
    [switch]$SkipBuild,
    [switch]$AllowDirty
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $RepoRoot

function Run-Step([string]$Name, [scriptblock]$Body) {
    Write-Host ""
    Write-Host "== $Name =="
    & $Body
}

function Require-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name is required but was not found in PATH."
    }
}

Require-Command git
Require-Command ssh
Require-Command scp

if (-not $AllowDirty) {
    $dirty = git status --porcelain
    if ($dirty) {
        throw "Working tree is not clean. Commit changes first, or rerun with -AllowDirty if you know what you are doing."
    }
}

if (-not $SkipTests) {
    Run-Step "npm test" { npm test }
}

if (-not $SkipBuild) {
    Run-Step "npm run build" { npm run build }
}

$sha = (git rev-parse --short HEAD).Trim()
$archive = Join-Path $env:TEMP "codex-deploy-$sha.zip"
if (Test-Path $archive) {
    Remove-Item -LiteralPath $archive -Force
}

Run-Step "create archive" {
    git archive --format=zip --output $archive HEAD
}

$remote = "$ServerUser@$ServerHost"
$remoteArchive = "/tmp/codex-deploy-$sha.zip"

Run-Step "upload archive" {
    scp $archive "${remote}:$remoteArchive"
}

$remoteScript = @"
set -e
mkdir -p "$RemotePath"
unzip -o "$remoteArchive" -d "$RemotePath"
cd "$RemotePath"
npm install
npm run build
rm -f "$remoteArchive"
"@

if ($RestartCommand) {
    $remoteScript += "`n$RestartCommand`n"
}

Run-Step "remote install" {
    $remoteScript | ssh $remote "bash -s"
}

Write-Host ""
Write-Host "Done. deployed commit $sha to ${remote}:$RemotePath"

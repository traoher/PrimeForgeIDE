<# 
.SYNOPSIS
    Syncs Proton9 agent source into PrimeForgeIDE/agent/.
    Run before building the IDE extension.
.USAGE
    .\sync-agent.ps1                         # auto-detect Proton9 sibling
    .\sync-agent.ps1 -Source "D:\Proton9"    # explicit path
#>
param(
    [string]$Source = ""
)

$ErrorActionPreference = "Stop"

# Auto-detect Proton9 as a sibling directory
if (-not $Source) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $ideRoot = Split-Path -Parent $scriptDir
    $Source = Join-Path (Split-Path -Parent $ideRoot) "Proton9"
}

$scriptDir2 = Split-Path -Parent $MyInvocation.MyCommand.Path
$agentDir = Join-Path (Split-Path -Parent $scriptDir2) "agent"

if (-not (Test-Path $Source)) {
    Write-Error "Proton9 source not found at: $Source"
    exit 1
}

Write-Host "Syncing Proton9 -> PrimeForgeIDE/agent/" -ForegroundColor Cyan
Write-Host "  Source: $Source"
Write-Host "  Target: $agentDir"

# Directories to sync (mirror contents, overwrite)
$dirs = @("core", "tools", "config")
foreach ($d in $dirs) {
    $src = Join-Path $Source $d
    $dst = Join-Path $agentDir $d
    if (Test-Path $src) {
        if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
        Copy-Item -Path $src -Destination $dst -Recurse -Force
        $count = @(Get-ChildItem $dst -Recurse -File).Count
        Write-Host "  [OK] $d/ ($count files)" -ForegroundColor Green
    } else {
        Write-Host "  [WARN] $d/ not found in source" -ForegroundColor Yellow
    }
}

# Individual files to sync
$files = @("proton9.py", "requirements.txt", "swe_bench_harness.py", ".env")
foreach ($f in $files) {
    $src = Join-Path $Source $f
    $dst = Join-Path $agentDir $f
    if (Test-Path $src) {
        Copy-Item -Path $src -Destination $dst -Force
        Write-Host "  [OK] $f" -ForegroundColor Green
    } else {
        Write-Host "  [WARN] $f not found in source" -ForegroundColor Yellow
    }
}

# Skip runtime dirs (.memory/, logs/) -- they are IDE-specific state

Write-Host ""
Write-Host "Agent sync complete!" -ForegroundColor Green

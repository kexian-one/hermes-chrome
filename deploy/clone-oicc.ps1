#Requires -Version 5.1
<#
.SYNOPSIS
    Clones open-claude-in-chrome N times into deploy/oicc-b1..bN with
    per-instance patches needed for parallel multi-browser operation:
      1. config.json with distinct port (18765..)
      2. .cmd launcher that sets OICC_PORT before invoking native-host.js
      3. background.js NATIVE_HOST_NAME suffixed with .b<N> (so each instance
         talks to its own registered native messaging host)
      4. manifest.json name renamed to "AI Chrome Assistant (b<N>)"
      5. mcp-server.js / native-host.js getPort() honors OICC_PORT env var
      6. npm ci in host/ (mcp-server.js needs its deps)

.PARAMETER Count
    Number of instances to create. Default: 6

.PARAMETER Force
    Wipe and re-clone existing instances. Without this, existing dirs are
    skipped for cloning, but patches are still re-applied idempotently.

.PARAMETER SkipNpmInstall
    Don't run `npm ci` in host/. Useful for CI smoke tests; for real
    use you need this to run at least once.

.PARAMETER WhatIf
    Print what would be done without doing anything.
#>
param(
    [ValidateRange(1, 6)]
    [int]$Count = 6,
    [switch]$Force,
    [switch]$SkipNpmInstall,
    [switch]$WhatIf
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoUrl   = 'https://github.com/noemica-io/open-claude-in-chrome.git'
$BasePort  = 18765
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PinnedRevision = (Get-Content -LiteralPath (Join-Path $ScriptDir 'oicc-revision.txt') -Raw).Trim()
$PatchPython = Join-Path (Split-Path $ScriptDir -Parent) '.venv\Scripts\python.exe'

function Write-Action {
    param([string]$Message)
    Write-Host "[WHATIF] $Message" -ForegroundColor Cyan
}

for ($i = 1; $i -le $Count; $i++) {
    $instanceName = "oicc-b$i"
    $instanceDir  = Join-Path $ScriptDir $instanceName
    $port         = $BasePort + ($i - 1)
    $cmdPath      = Join-Path $ScriptDir "$instanceName.cmd"

    if ($WhatIf) {
        Write-Action "Would clone $RepoUrl -> $instanceDir (if missing)"
        Write-Action "Would write config.json port=$port"
        Write-Action "Would write launcher $cmdPath with OICC_PORT=$port"
        Write-Action "Would patch extension\background.js NATIVE_HOST_NAME with .b$i suffix"
        Write-Action "Would patch extension\manifest.json name -> 'AI Chrome Assistant (b$i)'"
        Write-Action "Would patch host\tool-runtime.js getPort() to honor OICC_PORT"
        Write-Action "Would patch host\native-host.js getPort() to honor OICC_PORT"
        if (-not $SkipNpmInstall) {
            Write-Action "Would run 'npm ci' in $instanceDir\host"
        }
        continue
    }

    Write-Host "=== oicc-b$i (port $port) ===" -ForegroundColor Cyan

    # 1. Clone
    if (Test-Path $instanceDir) {
        if ($Force) {
            Write-Host "  Removing existing (Force)" -ForegroundColor Yellow
            $resolvedInstance = [System.IO.Path]::GetFullPath($instanceDir)
            $expectedInstance = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir "oicc-b$i"))
            if ($resolvedInstance -ne $expectedInstance -or (Get-Item -LiteralPath $instanceDir).Attributes.HasFlag([System.IO.FileAttributes]::ReparsePoint)) { throw "Unsafe instance path" }
            Remove-Item -LiteralPath $resolvedInstance -Recurse -Force
        } else {
            Write-Host "  Clone: exists, skip clone (patches still re-applied)"
        }
    }
    if (-not (Test-Path $instanceDir)) {
        Write-Host "  Cloning..."
        git clone --no-checkout --filter=blob:none $RepoUrl $instanceDir | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Clone failed' }
        git -C $instanceDir fetch --depth 1 origin $PinnedRevision
        if ($LASTEXITCODE -ne 0) { throw 'Pinned revision fetch failed' }
        git -C $instanceDir checkout --detach $PinnedRevision
        if ($LASTEXITCODE -ne 0) { throw 'Pinned revision checkout failed' }
    }

    $actualRevision = (git -C $instanceDir rev-parse HEAD).Trim()
    if ($actualRevision -ne $PinnedRevision) { throw "Existing instance uses another revision. Back it up before recreating with -Force." }

    # 2. config.json
    $configContent = "{`n  `"port`": $port`n}`n"
    [System.IO.File]::WriteAllText("$instanceDir\config.json", $configContent, [System.Text.Encoding]::UTF8)
    Write-Host "  config.json port=$port written"

    # 3. .cmd launcher with OICC_PORT
    $nodeHostRelative = 'host\native-host.js'
    $cmdContent = @"
@echo off
cd /d "%~dp0$instanceName"
set OICC_PORT=$port
node $nodeHostRelative %*
"@
    [System.IO.File]::WriteAllText($cmdPath, $cmdContent, [System.Text.Encoding]::ASCII)
    Write-Host "  launcher $cmdPath (OICC_PORT=$port)"

    & $PatchPython (Join-Path $ScriptDir 'patch_oicc.py') $instanceDir --instance $i
    if ($LASTEXITCODE -ne 0) { throw 'Browser compatibility patch failed' }

    # 5. npm ci
    if (-not $SkipNpmInstall) {
        $hostDir = Join-Path $instanceDir 'host'
        $nodeModules = Join-Path $hostDir 'node_modules'
        if (Test-Path $nodeModules) {
            Write-Host "  npm: node_modules exists, skipping"
        } else {
            Write-Host "  npm ci in $hostDir ..."
            Push-Location $hostDir
            try {
                if (Test-Path -LiteralPath 'package-lock.json') { npm ci --silent } else { throw 'Missing npm lock file' }
                if ($LASTEXITCODE -ne 0) { throw 'npm ci failed' }
                Write-Host "  npm ci done"
            } finally {
                Pop-Location
            }
        }
    }
}

if (-not $WhatIf) {
    Write-Host ""
    Write-Host "Done. Instances ready:" -ForegroundColor Green
    for ($i = 1; $i -le $Count; $i++) {
        $instanceDir = Join-Path $ScriptDir "oicc-b$i"
        $port        = $BasePort + ($i - 1)
        if (Test-Path $instanceDir) {
            Write-Host "  oicc-b$i  port=$port  $instanceDir"
        }
    }
    Write-Host ""
    Write-Host "Next: load deploy/oicc-b<N>/extension/ in a browser (developer mode),"
    Write-Host "then run deploy/register-native-host.ps1 with the extension ID." -ForegroundColor DarkGray
}

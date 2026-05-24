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
      6. npm install in host/ (mcp-server.js needs its deps)

.PARAMETER Count
    Number of instances to create. Default: 6

.PARAMETER Force
    Wipe and re-clone existing instances. Without this, existing dirs are
    skipped for cloning, but patches are still re-applied idempotently.

.PARAMETER SkipNpmInstall
    Don't run `npm install` in host/. Useful for CI smoke tests; for real
    use you need this to run at least once.

.PARAMETER WhatIf
    Print what would be done without doing anything.
#>
param(
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

function Write-Action {
    param([string]$Message)
    Write-Host "[WHATIF] $Message" -ForegroundColor Cyan
}

function Update-File {
    param(
        [string]$Path,
        [string]$Pattern,        # regex
        [string]$Replacement,
        [string]$Description
    )
    if (-not (Test-Path $Path)) {
        Write-Host "    skip $Description : file missing $Path" -ForegroundColor DarkYellow
        return
    }
    $content = Get-Content $Path -Raw -Encoding utf8
    $new = $content -replace $Pattern, $Replacement
    if ($content -eq $new) {
        Write-Host "    $Description : already applied"
    } else {
        Set-Content -Path $Path -Value $new -Encoding utf8 -NoNewline
        Write-Host "    $Description : patched"
    }
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
        Write-Action "Would patch host\mcp-server.js getPort() to honor OICC_PORT"
        Write-Action "Would patch host\native-host.js getPort() to honor OICC_PORT"
        if (-not $SkipNpmInstall) {
            Write-Action "Would run 'npm install' in $instanceDir\host"
        }
        continue
    }

    Write-Host "=== oicc-b$i (port $port) ===" -ForegroundColor Cyan

    # 1. Clone
    if (Test-Path $instanceDir) {
        if ($Force) {
            Write-Host "  Removing existing (Force)" -ForegroundColor Yellow
            Remove-Item -Recurse -Force $instanceDir
        } else {
            Write-Host "  Clone: exists, skip clone (patches still re-applied)"
        }
    }
    if (-not (Test-Path $instanceDir)) {
        Write-Host "  Cloning..."
        git clone --depth 1 $RepoUrl $instanceDir | Out-Null
    }

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

    # 4. Patches
    Write-Host "  Patches:"
    Update-File `
        -Path "$instanceDir\extension\background.js" `
        -Pattern 'const NATIVE_HOST_NAME = "com\.anthropic\.open_claude_in_chrome";?' `
        -Replacement "const NATIVE_HOST_NAME = `"com.anthropic.open_claude_in_chrome.b$i`";" `
        -Description "background.js NATIVE_HOST_NAME -> .b$i"

    Update-File `
        -Path "$instanceDir\extension\manifest.json" `
        -Pattern '"name": "Open Claude in Chrome"' `
        -Replacement "`"name`": `"AI Chrome Assistant (b$i)`"" `
        -Description "manifest.json name -> AI Chrome Assistant (b$i)"

    $envHookMcp = "function getPort() {`r`n  if (process.env.OICC_PORT) {`r`n    const p = parseInt(process.env.OICC_PORT, 10);`r`n    if (!isNaN(p) && p > 0) return p;`r`n  }`r`n  const configPath ="
    Update-File `
        -Path "$instanceDir\host\mcp-server.js" `
        -Pattern 'function getPort\(\) \{\r?\n  const configPath =' `
        -Replacement $envHookMcp `
        -Description "mcp-server.js getPort honors OICC_PORT"

    $envHookNative = "function getPort() {`r`n  if (process.env.OICC_PORT) {`r`n    const p = parseInt(process.env.OICC_PORT, 10);`r`n    if (!isNaN(p) && p > 0) return p;`r`n  }`r`n  const configPath = path.join("
    Update-File `
        -Path "$instanceDir\host\native-host.js" `
        -Pattern 'function getPort\(\) \{\r?\n  const configPath = path\.join\(' `
        -Replacement $envHookNative `
        -Description "native-host.js getPort honors OICC_PORT"

    # 5. npm install
    if (-not $SkipNpmInstall) {
        $hostDir = Join-Path $instanceDir 'host'
        $nodeModules = Join-Path $hostDir 'node_modules'
        if (Test-Path $nodeModules) {
            Write-Host "  npm: node_modules exists, skipping"
        } else {
            Write-Host "  npm install in $hostDir ..."
            Push-Location $hostDir
            try {
                npm install --silent 2>&1 | Out-Null
                Write-Host "  npm install done"
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

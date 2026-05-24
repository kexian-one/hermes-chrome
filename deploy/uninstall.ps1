#Requires -Version 5.1
<#
.SYNOPSIS
    Reverses clone-oicc.ps1 and register-native-host.ps1:
    removes HKCU registry keys for all browsers/instances,
    deletes oicc-b1..b6 dirs and .cmd launchers.

.PARAMETER Count
    Number of instances to remove. Default: 6

.PARAMETER WhatIf
    Print what would be removed without removing anything.
#>
param(
    [int]$Count = 6,
    [switch]$WhatIf
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

$BrowserVendors = @{
    Chrome   = 'Google\Chrome'
    Edge     = 'Microsoft\Edge'
    Brave    = 'BraveSoftware\Brave-Browser'
    Vivaldi  = 'Vivaldi'
    Opera    = 'Opera Software\Opera Stable'
    Chromium = 'Chromium'
}

function Write-Action {
    param([string]$Message)
    Write-Host "[WHATIF] $Message" -ForegroundColor Cyan
}

Write-Host "=== Uninstall: registry keys ===" -ForegroundColor Yellow

for ($i = 1; $i -le $Count; $i++) {
    $HostName = "com.anthropic.open_claude_in_chrome.b$i"
    foreach ($browser in $BrowserVendors.Keys) {
        $vendor  = $BrowserVendors[$browser]
        $regPath = "HKCU:\Software\$vendor\NativeMessagingHosts\$HostName"
        if ($WhatIf) {
            Write-Action "Would remove registry key: $regPath"
            continue
        }
        if (Test-Path $regPath) {
            Remove-Item -Path $regPath -Recurse -Force
            Write-Host "Removed: $regPath"
        }
    }
}

Write-Host ""
Write-Host "=== Uninstall: clone directories and launchers ===" -ForegroundColor Yellow

for ($i = 1; $i -le $Count; $i++) {
    $instanceDir = Join-Path $ScriptDir "oicc-b$i"
    $cmdPath     = Join-Path $ScriptDir "oicc-b$i.cmd"

    if ($WhatIf) {
        Write-Action "Would delete dir: $instanceDir"
        Write-Action "Would delete cmd: $cmdPath"
        continue
    }

    if (Test-Path $instanceDir) {
        Remove-Item -Recurse -Force $instanceDir
        Write-Host "Deleted: $instanceDir"
    }

    if (Test-Path $cmdPath) {
        Remove-Item -Force $cmdPath
        Write-Host "Deleted: $cmdPath"
    }
}

if (-not $WhatIf) {
    Write-Host ""
    Write-Host "Uninstall complete." -ForegroundColor Green
}

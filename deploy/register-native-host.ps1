#Requires -Version 5.1
<#
.SYNOPSIS
    Registers the native messaging host manifest for a given browser+instance pair.
    Writes the manifest JSON and creates the HKCU registry key pointing to it.
    No admin elevation required (HKCU).

.PARAMETER Browser
    One of: Chrome, Edge, Brave, Vivaldi, Opera, Chromium

.PARAMETER Instance
    Instance number 1-6 (corresponds to oicc-b1..b6).

.PARAMETER ExtensionId
    The unpacked extension ID as shown in chrome://extensions.
    Can also be stored in deploy/oicc-b{n}/extension-id.txt and omitted here.

.PARAMETER WhatIf
    Print what would be written without writing anything.
#>
param(
    [Parameter(Mandatory)]
    [ValidateSet('Chrome','Edge','Brave','Vivaldi','Opera','Chromium')]
    [string]$Browser,

    [Parameter(Mandatory)]
    [ValidateRange(1,6)]
    [int]$Instance,

    [string]$ExtensionId,

    [switch]$WhatIf
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$HostName    = "com.anthropic.open_claude_in_chrome.b$Instance"
$InstanceDir = Join-Path $ScriptDir "oicc-b$Instance"
$ManifestDir = Join-Path $InstanceDir 'manifest'
$ManifestPath = Join-Path $ManifestDir "$HostName.json"
$CmdLauncher  = Join-Path $ScriptDir "oicc-b$Instance.cmd"

$BrowserVendors = @{
    Chrome   = 'Google\Chrome'
    Edge     = 'Microsoft\Edge'
    Brave    = 'BraveSoftware\Brave-Browser'
    Vivaldi  = 'Vivaldi'
    Opera    = 'Opera Software\Opera Stable'
    Chromium = 'Chromium'
}

$Vendor  = $BrowserVendors[$Browser]
$RegPath = "HKCU:\Software\$Vendor\NativeMessagingHosts\$HostName"

if (-not $ExtensionId) {
    $idFile = Join-Path $InstanceDir 'extension-id.txt'
    if (Test-Path $idFile) {
        $ExtensionId = (Get-Content $idFile -Raw).Trim()
        Write-Host "Read ExtensionId from $idFile : $ExtensionId"
    } else {
        Write-Error "ExtensionId not provided and $idFile not found. Load the extension in $Browser first, then pass -ExtensionId <id> or save the id to $idFile."
    }
}

$AllowedOrigin = "chrome-extension://$ExtensionId/"

$ManifestObject = [ordered]@{
    name            = $HostName
    description     = "Open Claude in Chrome (instance b$Instance)"
    path            = $CmdLauncher
    type            = 'stdio'
    allowed_origins = @($AllowedOrigin)
}

$ManifestJson = $ManifestObject | ConvertTo-Json -Depth 4

function Write-Action {
    param([string]$Label, [string]$Value)
    Write-Host ("[WHATIF] {0,-30} {1}" -f $Label, $Value) -ForegroundColor Cyan
}

if ($WhatIf) {
    Write-Action "Registry key:"    $RegPath
    Write-Action "  default value:" $ManifestPath
    Write-Action "Manifest file:"   $ManifestPath
    Write-Host ""
    Write-Host "Manifest content that would be written:" -ForegroundColor Cyan
    Write-Host $ManifestJson
    return
}

if (-not (Test-Path $ManifestDir)) {
    New-Item -ItemType Directory -Path $ManifestDir | Out-Null
}

[System.IO.File]::WriteAllText($ManifestPath, $ManifestJson, [System.Text.Encoding]::UTF8)
Write-Host "Wrote manifest: $ManifestPath"

if (-not (Test-Path $RegPath)) {
    New-Item -Path $RegPath -Force | Out-Null
}
Set-ItemProperty -Path $RegPath -Name '(default)' -Value $ManifestPath
Write-Host "Set registry:   $RegPath = $ManifestPath"
Write-Host ""
Write-Host "Registered $HostName for $Browser (instance b$Instance)" -ForegroundColor Green

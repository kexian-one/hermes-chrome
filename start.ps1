# start.ps1 — restart master orchestrator on this machine
#
# What it does (in order):
#   1. Kill any running python.exe whose command line mentions agent.master /
#      agent.worker / agent.bot — these are this project's processes.
#   2. Kill any running node.exe whose command line mentions
#      deploy/oicc-*/host/mcp-server.js — these are the PRIMARY MCP keepalives
#      spawned by the old master.
#   3. Self-heal native messaging registry entries (HKCU). Edge auto-update
#      occasionally wipes sideloaded extensions' native host registry; this
#      step ensures the entry pointed at by the existing manifest under
#      deploy\oicc-bN\manifest\ is present in the right browser's HKCU node.
#   4. Ensure logs/ exists.
#   5. Launch `python -m agent.master` detached, redirecting stdout+stderr to
#      logs/master.log (append).
#   6. Print the new master PID and how to tail the log.
#
# Usage:  powershell -ExecutionPolicy Bypass -File .\start.ps1
# or just:  .\start.ps1   (from a PowerShell prompt in d:\ai\all-in-ai)

$ErrorActionPreference = 'Stop'

# Pin working dir to this script's location so relative paths in master
# (state/, logs/, config.yaml) resolve correctly even if launched from
# another folder.
Set-Location -Path $PSScriptRoot

Write-Host "[start.ps1] killing old processes..."

$pythonPattern = 'agent\.(master|worker|bot)'
$nodePattern   = 'deploy[\\/]oicc-\w+[\\/]host[\\/]mcp-server\.js'

$killed = @()
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='node.exe'" |
    Where-Object {
        ($_.Name -eq 'python.exe' -and $_.CommandLine -match $pythonPattern) -or
        ($_.Name -eq 'node.exe'   -and $_.CommandLine -match $nodePattern)
    } |
    ForEach-Object {
        $pidNum = $_.ProcessId
        $name  = $_.Name
        try {
            Stop-Process -Id $pidNum -Force -ErrorAction Stop
            $killed += "  killed pid=$pidNum ($name)"
        } catch {
            $killed += "  FAILED to kill pid=$pidNum ($name): $($_.Exception.Message)"
        }
    }

if ($killed.Count -eq 0) {
    Write-Host "  (no existing master/worker/mcp processes)"
} else {
    $killed | ForEach-Object { Write-Host $_ }
}

# Give the OS a moment to release sockets (TCP 18766 etc.) before relaunch.
Start-Sleep -Milliseconds 800

# --- Native messaging registry self-heal ----------------------------------
# Read config.yaml's `browsers:` block to learn which Chromium variant each
# worker uses (b2→edge, b3→chrome, …), then make sure
# HKCU:\Software\<vendor>\NativeMessagingHosts\com.anthropic.open_claude_in_chrome.b<N>
# points to the manifest file under deploy\oicc-b<N>\manifest\.
#
# Why this is here: Edge auto-updates sometimes wipe sideloaded extensions'
# HKCU entries during a PC restart, breaking native messaging silently. The
# extension keeps retrying connectNative every 24s but Edge has no host to
# launch. Re-asserting the key is idempotent and cheap.
$BrowserVendors = @{
    edge     = 'Microsoft\Edge'
    chrome   = 'Google\Chrome'
    brave    = 'BraveSoftware\Brave-Browser'
    vivaldi  = 'Vivaldi'
    opera    = 'Opera Software\Opera Stable'
    chromium = 'Chromium'
}

# Cheap config.yaml parser — we only need `browsers.<bN>.name`. Full YAML
# parsing would mean adding a module; the format here is fixed/simple.
$configBrowsers = @{}
$currentWorker = $null
$inBrowsersBlock = $false
foreach ($line in Get-Content (Join-Path $PSScriptRoot 'config.yaml')) {
    if ($line -match '^browsers:\s*$') { $inBrowsersBlock = $true; continue }
    if ($inBrowsersBlock -and $line -match '^[a-zA-Z_]') { $inBrowsersBlock = $false }
    if (-not $inBrowsersBlock) { continue }
    if ($line -match '^\s{2}(b[1-6]):\s*$') {
        $currentWorker = $matches[1]
    } elseif ($currentWorker -and $line -match '^\s{4}name:\s*([a-z]+)\s*$') {
        $configBrowsers[$currentWorker] = $matches[1]
    }
}

Write-Host "[start.ps1] checking native-messaging registry..."
foreach ($kv in $configBrowsers.GetEnumerator()) {
    $worker  = $kv.Key
    $browser = $kv.Value
    $vendor  = $BrowserVendors[$browser]
    if (-not $vendor) {
        Write-Host ("  {0}: unknown browser '{1}' in config.yaml, skipping" -f $worker, $browser) -ForegroundColor Yellow
        continue
    }
    $hostName     = "com.anthropic.open_claude_in_chrome.$worker"
    $manifestPath = Join-Path $PSScriptRoot "deploy\oicc-$worker\manifest\$hostName.json"
    if (-not (Test-Path $manifestPath)) {
        Write-Host ("  {0}: manifest missing at {1}, skipping (run deploy\register-native-host.ps1)" -f $worker, $manifestPath) -ForegroundColor Yellow
        continue
    }
    $regPath = "HKCU:\Software\$vendor\NativeMessagingHosts\$hostName"
    $existing = $null
    if (Test-Path $regPath) {
        try { $existing = (Get-ItemProperty -Path $regPath -Name '(default)' -ErrorAction Stop).'(default)' } catch {}
    } else {
        New-Item -Path $regPath -Force | Out-Null
    }
    if ($existing -eq $manifestPath) {
        Write-Host ("  {0} ({1}): OK" -f $worker, $browser)
    } else {
        Set-ItemProperty -Path $regPath -Name '(default)' -Value $manifestPath
        if ($null -eq $existing) {
            Write-Host ("  {0} ({1}): created -> {2}" -f $worker, $browser, $manifestPath) -ForegroundColor Green
        } else {
            Write-Host ("  {0} ({1}): repaired (was {2})" -f $worker, $browser, $existing) -ForegroundColor Green
        }
    }
}

# Ensure log dir
if (-not (Test-Path 'logs')) {
    New-Item -ItemType Directory -Path 'logs' | Out-Null
}

# master.py routes Python's logging via stderr (logging.basicConfig
# stream=sys.stderr). stdout is sparse status prints. We keep two files so
# Start-Process can redirect (it forbids identical paths for the two streams).
$logErr = Join-Path $PSScriptRoot 'logs\master.err.log'
$logOut = Join-Path $PSScriptRoot 'logs\master.out.log'

$ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $logErr -Value "`n========== restart $ts ==========" -Encoding utf8
Add-Content -Path $logOut -Value "`n========== restart $ts ==========" -Encoding utf8

Write-Host "[start.ps1] launching master..."

$proc = Start-Process `
    -FilePath 'python' `
    -ArgumentList '-u', '-m', 'agent.master' `
    -WorkingDirectory $PSScriptRoot `
    -RedirectStandardOutput $logOut `
    -RedirectStandardError  $logErr `
    -WindowStyle Hidden `
    -PassThru

Start-Sleep -Milliseconds 500

if ($proc.HasExited) {
    Write-Host "[start.ps1] master exited immediately (code=$($proc.ExitCode)). Check $logErr" -ForegroundColor Red
    exit 1
}

Write-Host ("[start.ps1] master started, pid={0}" -f $proc.Id) -ForegroundColor Green
Write-Host "[start.ps1] main log (stderr): $logErr"
Write-Host "[start.ps1] stdout log:         $logOut"
Write-Host "[start.ps1] tail with:  Get-Content -Path $logErr -Wait -Tail 50"

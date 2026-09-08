# Restart this project master with the locked environment; active workers must finish first.
# Keeps the independently supervised browser bridge running. Use -DryRun to inspect.

[CmdletBinding(SupportsShouldProcess)]
param([switch]$DryRun)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Run .\setup.ps1 first to create the locked Python environment.' }
$taskConfig = if ($env:ALL_IN_AI_CONFIG) { $env:ALL_IN_AI_CONFIG } else { Join-Path $PSScriptRoot 'config.yaml' }
if ($DryRun -or $WhatIfPreference) {
    Write-Host "DRY RUN: project=$PSScriptRoot; python=$taskPython; config=$taskConfig"
    Write-Host 'Check active workers; restart this project master; ensure OICC bridge; run browser smoke checks.'
    return
}
$taskRootPattern = [regex]::Escape($PSScriptRoot)
$taskProcesses = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -match $taskRootPattern -and $_.CommandLine -match 'agent\.(master|worker|bot)'
})
if (@($taskProcesses | Where-Object { $_.CommandLine -match 'agent\.worker' }).Count -gt 0) {
    throw 'This project has active workers. Wait for completion before restarting.'
}
foreach ($taskProcess in $taskProcesses) {
    if ($PSCmdlet.ShouldProcess("PID $($taskProcess.ProcessId)", 'Restart project master')) { Stop-Process -Id $taskProcess.ProcessId -Force }
}
& $taskPython -m scripts.oicc_bridge start
if ($LASTEXITCODE -ne 0) { Write-Warning 'Browser bridge is unavailable. Sourcing will use its static/API fallback.' }

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
foreach ($line in Get-Content -LiteralPath $taskConfig) {
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
    -FilePath $taskPython `
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

& $taskPython -m scripts.browser_tab_smoke --require-listener --timeout 15
if ($LASTEXITCODE -ne 0) { Write-Warning "Some browser workers are unavailable; check extension setup before live SKU validation." }

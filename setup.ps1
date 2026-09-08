[CmdletBinding()]
param([switch]$DryRun)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ($DryRun) { Write-Host 'uv sync --frozen --extra dev --link-mode copy'; return }
$taskUv = Get-Command uv -ErrorAction SilentlyContinue
if ($taskUv) {
    & $taskUv.Source sync --frozen --extra dev --link-mode copy
} elseif (Test-Path -LiteralPath '.venv\Scripts\uv.exe') {
    & '.venv\Scripts\uv.exe' sync --frozen --extra dev --link-mode copy
} else {
    python -m pip install --user uv==0.12.10
    if ($LASTEXITCODE -ne 0) { throw 'Could not install uv.' }
    python -m uv sync --frozen --extra dev --link-mode copy
}
if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed.' }

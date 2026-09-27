param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & $Python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
}
& '.\.venv\Scripts\python.exe' -m pip install -e '.[dev]'
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw 'Run tools\Setup.ps1 first.'
}
Start-Process -FilePath $taskPython -ArgumentList 'collector_app.py' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden

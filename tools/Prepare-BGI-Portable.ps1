param([string]$InstalledBGI = 'C:\Program Files\BetterGI')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskSource = Join-Path $taskRoot '.external\bettergi\BetterGenshinImpact\bin\x64\Release\net8.0-windows10.0.22621.0'
$taskDestination = Join-Path $taskRoot '.external\bettergi-portable'
if (-not (Test-Path -LiteralPath (Join-Path $taskSource 'BetterGI.exe'))) { throw 'Run Build-BGI.ps1 first' }
if (Test-Path -LiteralPath $taskDestination) { throw 'Portable destination already exists; existing configuration is preserved' }
Copy-Item -LiteralPath $taskSource -Destination $taskDestination -Recurse
if (Test-Path -LiteralPath (Join-Path $InstalledBGI 'User')) {
    Get-ChildItem -LiteralPath (Join-Path $InstalledBGI 'User') | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $taskDestination 'User') -Recurse -Force
    }
}
Write-Output $taskDestination

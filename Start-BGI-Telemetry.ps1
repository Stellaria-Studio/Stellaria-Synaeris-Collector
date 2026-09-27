param([int]$Port = 18765, [string]$Group = '')
$ErrorActionPreference = 'Stop'
$taskBGI = Join-Path $PSScriptRoot '.external\bettergi-portable\BetterGI.exe'
if (-not (Test-Path -LiteralPath $taskBGI)) { throw 'Run tools\Build-BGI.ps1 and tools\Prepare-BGI-Portable.ps1 first.' }
if (-not (Get-Process -Name YuanShen,GenshinImpact -ErrorAction SilentlyContinue)) { throw 'Enter Genshin first. Windows UAC/login must be handled locally.' }
if (Get-Process -Name BetterGI -ErrorAction SilentlyContinue) { throw 'Close the existing BGI instance before launching the telemetry build.' }
@{ url = "http://127.0.0.1:$Port" } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path (Split-Path -Parent $taskBGI) 'synaeris.telemetry.json') -Encoding utf8
$taskPreviousURL = $env:SYNAERIS_TELEMETRY_URL
try {
    $env:SYNAERIS_TELEMETRY_URL = "http://127.0.0.1:$Port"
    $taskArguments = if ($Group) { @('--startGroups', ('"'+$Group+'"')) } else { @('start') }
    Start-Process -FilePath $taskBGI -ArgumentList $taskArguments -WorkingDirectory (Split-Path -Parent $taskBGI) -WindowStyle Hidden
}
finally { $env:SYNAERIS_TELEMETRY_URL = $taskPreviousURL }

param([ValidateSet('nodkrai','natlan','mondstadt')][string]$Stage = 'nodkrai', [switch]$Remaining,
    [string]$CampaignPath = '')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
if (-not $Remaining -and (Get-Process -Name BetterGI -ErrorAction SilentlyContinue)) {
    Write-Error 'Close the existing BGI instance first; the collection launcher starts the telemetry instance.'
    exit 2
}
if (-not (Get-Process -Name YuanShen,GenshinImpact -ErrorAction SilentlyContinue)) {
    Write-Error 'Open Genshin and enter the game world first.'
    exit 2
}
$taskScript = Join-Path $taskRoot 'Start-Live-Collection.ps1'
if ($Remaining) { $taskScript = Join-Path $PSScriptRoot 'Run-Machine-Campaign.ps1' }
$taskPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$taskAttemptDirectory = Join-Path $taskRoot 'reports\local\launch'
New-Item -ItemType Directory -Path $taskAttemptDirectory -Force | Out-Null
$taskAttemptPath = Join-Path $taskAttemptDirectory ([guid]::NewGuid().ToString('N')+'.authorization.json')
$taskAttempt = @{ started_utc=[DateTime]::UtcNow.ToString('o'); stage=$Stage; remaining=[bool]$Remaining; status='awaiting_windows_authorization' }
if ($CampaignPath) {
    if (-not $Remaining) { throw 'A queue requires the continuous campaign launcher' }
    $CampaignPath = (Resolve-Path -LiteralPath $CampaignPath).Path
    $taskAttempt.campaign_path=$CampaignPath
}
$taskAttempt | ConvertTo-Json | Set-Content -LiteralPath $taskAttemptPath -Encoding UTF8
# Normal Windows authorization only. No policy changes, password handling or UAC bypass.
try {
    $taskArguments = if ($Remaining) {
        @('-NoProfile','-File',('"'+$taskScript+'"'),'-MinutesPerStage','10','-DeployBuild')
    } else { @('-NoProfile','-File',('"'+$taskScript+'"'),'-Stage',$Stage,'-Minutes','10') }
    if ($CampaignPath) { $taskArguments += @('-CampaignPath',('"'+$CampaignPath+'"'),'-SessionMinutes','120') }
    Start-Process -FilePath $taskPowerShell -Verb RunAs -WindowStyle Hidden -ArgumentList $taskArguments
    $taskAttempt.status='dispatched'; $taskAttempt | ConvertTo-Json | Set-Content -LiteralPath $taskAttemptPath -Encoding UTF8
} catch {
    $taskAttempt.status='not_dispatched'; $taskAttempt.error=$_.Exception.Message
    $taskAttempt | ConvertTo-Json | Set-Content -LiteralPath $taskAttemptPath -Encoding UTF8
    Write-Error 'Windows launch authorization was not completed. No automatic retry was made.'
    exit 2
}

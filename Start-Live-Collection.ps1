param(
    [string]$Stage = 'nodkrai',
    [int]$Minutes = 10,
    [switch]$OnlyCollector,
    [string]$CampaignPath = '',
    [string]$CampaignStopFile = ''
)
$ErrorActionPreference = 'Stop'
$taskLaunchDirectory = Join-Path $PSScriptRoot 'reports\local\launch'
New-Item -ItemType Directory -Path $taskLaunchDirectory -Force | Out-Null
$taskLaunchLog = Join-Path $taskLaunchDirectory ([guid]::NewGuid().ToString('N')+'.log')
Start-Transcript -LiteralPath $taskLaunchLog -Force | Out-Null
try {
Set-Location -LiteralPath $PSScriptRoot
if ($Minutes -lt 1 -or $Minutes -gt 120) { throw 'Minutes must be between 1 and 120' }
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'This machine runs Genshin elevated. Run this launcher as administrator; Windows authorization must be handled locally'
}
if (-not (Get-Process -Name YuanShen,GenshinImpact -ErrorAction SilentlyContinue)) { throw 'Enter Genshin first' }
if (-not $OnlyCollector -and (Get-Process -Name BetterGI -ErrorAction SilentlyContinue)) {
    throw 'Close the existing BGI instance first so only the telemetry instance controls the game'
}
$taskCampaignPath = if ($CampaignPath) { (Resolve-Path -LiteralPath $CampaignPath).Path } else { Join-Path $PSScriptRoot 'data\campaign_round02\campaign.json' }
if (-not (Test-Path -LiteralPath $taskCampaignPath)) { throw 'Run .venv\Scripts\python.exe tools\prepare_campaign.py first' }
$taskCampaign = Get-Content -LiteralPath $taskCampaignPath -Raw -Encoding UTF8 | ConvertFrom-Json
$taskStageMatches = @($taskCampaign.stages | Where-Object id -eq $Stage)
if ($taskStageMatches.Count -ne 1 -or $Stage -notmatch '^[a-z0-9_]+$') { throw 'Unknown or ambiguous campaign stage' }
$taskStageInfo = $taskStageMatches[0]
if ($taskStageInfo.group -notmatch '^Synaeris_[A-Za-z0-9_]+$') { throw 'Invalid native collection group' }
$taskConfigPath = Join-Path (Split-Path -Parent $taskCampaignPath) ($Stage+'.collector.json')
$taskRun = Join-Path $PSScriptRoot ('reports\local\live\'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $taskRun -Force | Out-Null
if ($OnlyCollector) {
    $taskManualConfig = Get-Content -LiteralPath $taskConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskManualConfig.actor = 'HUMAN'
    $taskConfigPath = Join-Path $taskRun 'collector.json'
    $taskManualConfig | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $taskConfigPath -Encoding utf8
}
try {
    $taskExistingHealth = Invoke-RestMethod 'http://127.0.0.1:18765/health' -TimeoutSec 1
} catch { $taskExistingHealth = $null }
if ($taskExistingHealth.status -eq 'recording') { throw 'Another collector already owns the bridge port; stop it first' }
$taskStop = Join-Path $taskRun 'stop.request'
$taskPreviousEncoding = $env:PYTHONIOENCODING
$taskPreviousPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $PSScriptRoot 'src'
    $env:PYTHONIOENCODING = 'utf-8'
    $taskArgs = @('-u','-m','synaeris_collector.cli','record','--config',('"'+$taskConfigPath+'"'),
        '--duration',($Minutes*60),'--wait-game','600','--restore-game','--stop-file',('"'+$taskStop+'"'))
    $taskProcess = Start-Process -FilePath (Join-Path $PSScriptRoot '.venv\Scripts\python.exe') `
        -ArgumentList $taskArgs -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $taskRun 'collector.log') `
        -RedirectStandardError (Join-Path $taskRun 'collector.err.log')
    Write-Host "Collector PID $($taskProcess.Id). Switch to the restored game world for preflight."
    Write-Host "Safe stop: create $taskStop"
    $taskDeadline = [DateTime]::UtcNow.AddSeconds(600)
    $taskReady = $false
    while ([DateTime]::UtcNow -lt $taskDeadline -and -not $taskProcess.HasExited) {
        if ($CampaignStopFile -and (Test-Path -LiteralPath $CampaignStopFile)) { throw 'Campaign stop requested during game preflight' }
        try {
            $taskHealth = Invoke-RestMethod 'http://127.0.0.1:18765/health' -TimeoutSec 2
            if ($taskHealth.status -eq 'recording') { $taskReady = $true; break }
        } catch { }
        Start-Sleep -Milliseconds 250
        $taskProcess.Refresh()
    }
    if (-not $taskReady) { throw "Collector preflight failed; inspect $taskRun" }
    if (-not $OnlyCollector) { & (Join-Path $PSScriptRoot 'Start-BGI-Telemetry.ps1') -Group $taskStageInfo.group }
    Write-Host "Recording stage $Stage. A BGI return is not a verified success."
    Write-Host "Collection duration is bounded; stopping recording does not stop BGI."
    [pscustomobject]@{ RunDirectory = $taskRun; CollectorPid = $taskProcess.Id; StopFile = $taskStop; Stage = $Stage }
} catch {
    New-Item -ItemType File -Path $taskStop -Force | Out-Null
    throw
} finally {
    $env:PYTHONIOENCODING = $taskPreviousEncoding
    $env:PYTHONPATH = $taskPreviousPath
}
} catch {
    Write-Host ('Launch failed: '+$_.Exception.Message)
    throw
} finally {
    Stop-Transcript | Out-Null
}

param([int]$MinutesPerStage = 10, [switch]$DeployBuild,
    [string]$CampaignPath = '', [int]$SessionMinutes = 120,
    [string]$ResumeReport = '')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
if ($MinutesPerStage -lt 1 -or $MinutesPerStage -gt 120) { throw 'Invalid stage duration' }
if ($SessionMinutes -lt 1 -or $SessionMinutes -gt 120) { throw 'Invalid session duration' }
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run the campaign with ordinary Windows administrator authorization'
}
$taskPortable = Join-Path $taskRoot '.external\bettergi-portable'
$taskExecutable = Join-Path $taskPortable 'BetterGI.exe'
$taskCampaignPath = if ($CampaignPath) { (Resolve-Path -LiteralPath $CampaignPath).Path } else { Join-Path $taskRoot 'data\campaign_round02\campaign.json' }
$taskCampaign = Get-Content -LiteralPath $taskCampaignPath -Raw -Encoding UTF8 | ConvertFrom-Json
$taskCampaignHash = (Get-FileHash -LiteralPath $taskCampaignPath -Algorithm SHA256).Hash
$taskReportDirectory = Join-Path $taskRoot 'reports\local\campaign'
New-Item -ItemType Directory -Path $taskReportDirectory -Force | Out-Null
$taskReportPath = Join-Path $taskReportDirectory ([guid]::NewGuid().ToString('N')+'.json')
$taskResults = @()
$taskOwnsBGI = $false
$taskStateStatus = 'starting'
$taskDeadline = [DateTime]::UtcNow.AddMinutes($SessionMinutes)
$taskStopCampaign = Join-Path $taskReportDirectory 'stop.request'
if (Test-Path -LiteralPath $taskStopCampaign) { throw 'Campaign stop request is present; preserved until deliberately removed' }
if ($ResumeReport) {
    $taskPrior = Get-Content -LiteralPath $ResumeReport -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($taskPrior.campaign_sha256 -ne $taskCampaignHash) { throw 'Resume campaign does not match the immutable queue' }
    $taskResults = @($taskPrior.stages)
}
function Save-CampaignState {
    $taskState = @{ schema_version=2; status=$taskStateStatus; stages=$taskResults; verified_success=$false;
        campaign_path=$taskCampaignPath; campaign_sha256=$taskCampaignHash;
        pid=$PID; deadline_utc=$taskDeadline.ToString('o'); stop_file=$taskStopCampaign;
        updated_utc=[DateTime]::UtcNow.ToString('o'); training_hours_verified=0 }
    $taskState | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath ($taskReportPath+'.tmp') -Encoding UTF8
    Move-Item -LiteralPath ($taskReportPath+'.tmp') -Destination $taskReportPath -Force
    @{ report=$taskReportPath; pid=$PID; stop_file=$taskStopCampaign } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $taskReportDirectory 'latest.json') -Encoding UTF8
}
function Close-OwnedBGI {
    foreach ($taskBGIProcess in @(Get-Process -Name BetterGI -ErrorAction SilentlyContinue)) {
        if (-not $taskBGIProcess.Path -or -not [string]::Equals($taskBGIProcess.Path,$taskExecutable,[StringComparison]::OrdinalIgnoreCase)) {
            throw 'Another BGI installation is running; it was preserved'
        }
        if (-not $taskBGIProcess.CloseMainWindow()) { throw 'Telemetry BGI did not accept a normal close request' }
        if (-not $taskBGIProcess.WaitForExit(15000)) { throw 'Telemetry BGI did not close; no force termination performed' }
    }
}
try {
    Save-CampaignState
    try { $taskActive = Invoke-RestMethod 'http://127.0.0.1:18765/health' -TimeoutSec 1 } catch { $taskActive=$null }
    if ($taskActive.status -eq 'recording') { throw 'An episode is still recording; finalize it before this campaign' }
    Close-OwnedBGI
    $taskOwnsBGI = $true
    if ($DeployBuild) {
        $taskBuild = Join-Path $taskRoot '.external\bettergi\BetterGenshinImpact\bin\x64\Release\net8.0-windows10.0.22621.0'
        $taskQualification = Get-Content -LiteralPath (Join-Path $taskRoot 'reports\local\bgi-qualified.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($taskQualification.status -ne 'build_passed') { throw 'Build qualification is missing' }
        foreach ($taskBinary in @('BetterGI.exe','BetterGI.dll','BetterGI.deps.json','BetterGI.runtimeconfig.json','Fischless.WindowsInput.dll')) {
            if (-not (Test-Path -LiteralPath (Join-Path $taskBuild $taskBinary))) { throw "Missing qualified build file: $taskBinary" }
            if ((Get-FileHash -LiteralPath (Join-Path $taskBuild $taskBinary) -Algorithm SHA256).Hash -ne $taskQualification.files.$taskBinary) { throw "Build artifact changed: $taskBinary" }
        }
        foreach ($taskBinary in @('BetterGI.exe','BetterGI.dll','BetterGI.deps.json','BetterGI.runtimeconfig.json','Fischless.WindowsInput.dll')) {
            Copy-Item -LiteralPath (Join-Path $taskBuild $taskBinary) -Destination (Join-Path $taskPortable $taskBinary) -Force
        }
    }
    # Immutable native collection queue; no general command or input endpoint.
    $taskStageQueue = if ($CampaignPath) { @($taskCampaign.stages) } else { @($taskCampaign.stages | Where-Object id -in @('natlan','mondstadt')) }
    foreach ($taskStageInfo in $taskStageQueue) {
        $taskStage = $taskStageInfo.id
        $taskRunInfo = $null
        if (@($taskResults | Where-Object { $_.stage -eq $taskStage -and $_.status -eq 'technical_qualified_review_pending' }).Count) { continue }
        if ((Test-Path -LiteralPath $taskStopCampaign) -or [DateTime]::UtcNow -ge $taskDeadline) { $taskStateStatus='bounded_or_requested_stop'; break }
        if ($taskStageInfo.group_sha256) {
            $taskGroupPath = Join-Path $taskPortable ('User\ScriptGroup\'+$taskStageInfo.group+'.json')
            if ($taskStageInfo.group_semantic_sha256) {
                $env:PYTHONPATH = Join-Path $taskRoot 'src'
                & (Join-Path $taskRoot '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'check_native_group.py') $taskGroupPath $taskStageInfo.group_semantic_sha256
                if ($LASTEXITCODE -ne 0) { throw 'Native execution configuration changed after preparation' }
            } elseif ((Get-FileHash -LiteralPath $taskGroupPath -Algorithm SHA256).Hash -ne $taskStageInfo.group_sha256) { throw 'Native group changed after preparation' }
            $taskRouteManifest = Get-Content -LiteralPath $taskStageInfo.route_manifest -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($taskRoute in $taskRouteManifest.routes) {
                $taskAsset = Join-Path $taskPortable ('User\AutoPathing\StellariaSynaeris\'+$taskStageInfo.group+'\'+$taskRoute.name)
                if ((Get-FileHash -LiteralPath $taskAsset -Algorithm SHA256).Hash -ne $taskRoute.sha256) { throw 'Native route changed after preparation' }
            }
        }
        $taskRemainingMinutes = [Math]::Floor(($taskDeadline-[DateTime]::UtcNow).TotalMinutes)
        if ($taskRemainingMinutes -lt 1) { $taskStateStatus='bounded_or_requested_stop'; break }
        $taskStageMinutes = [Math]::Min($MinutesPerStage,$taskRemainingMinutes)
        $taskRunInfo = & (Join-Path $taskRoot 'Start-Live-Collection.ps1') -Stage $taskStage -Minutes $taskStageMinutes -CampaignPath $taskCampaignPath -CampaignStopFile $taskStopCampaign
        $taskRecord = [ordered]@{ stage=$taskStage; run_directory=$taskRunInfo.RunDirectory; status='recording'; episode_id=$null; route_count=$taskStageInfo.route_count; verified_success=$false }
        $taskResults += $taskRecord
        $taskStateStatus='recording'
        Save-CampaignState
        $taskRecorder = Get-Process -Id $taskRunInfo.CollectorPid -ErrorAction Stop
        $taskLimit = [DateTime]::UtcNow.AddMinutes($taskStageMinutes).AddSeconds(30)
        $taskCompletion = $null
        while (-not $taskRecorder.HasExited -and [DateTime]::UtcNow -lt $taskLimit) {
            if ((Test-Path -LiteralPath $taskStopCampaign) -or [DateTime]::UtcNow -ge $taskDeadline) { break }
            try {
                $taskHealth = Invoke-RestMethod 'http://127.0.0.1:18765/health' -TimeoutSec 2
                $taskRecord.episode_id = $taskHealth.episode_id
                if ($taskHealth.completed_pathing_routes -ge $taskStageInfo.route_count -and $taskHealth.active_tool_count -eq 0) {
                    if (-not $taskCompletion) { $taskCompletion = [DateTime]::UtcNow }
                    if (([DateTime]::UtcNow-$taskCompletion).TotalSeconds -ge 8) { break }
                } else { $taskCompletion=$null }
            } catch { }
            Start-Sleep -Seconds 2
            $taskRecorder.Refresh()
        }
        New-Item -ItemType File -Path $taskRunInfo.StopFile -Force | Out-Null
        if (-not $taskRecorder.WaitForExit(20000)) { throw 'Recorder finalization did not finish; preserved without force termination' }
        $taskRecord.status = if ($taskCompletion) { 'routes_returned_unverified' } else { 'bounded_recording_ended' }
        if ($taskRecord.episode_id) {
            $taskManifestPath = Join-Path $taskRoot ('data\episodes\'+$taskRecord.episode_id+'\manifest.json')
            $taskManifest = Get-Content -LiteralPath $taskManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $taskRecord.episode_status=$taskManifest.status
            if ($taskManifest.status -ne 'complete') { throw "Episode failed: $($taskRecord.episode_id)" }
        }
        Close-OwnedBGI
        if (Test-Path -LiteralPath $taskStopCampaign) {
            $taskRecord.status='requested_stop_preserved'; $taskStateStatus='bounded_or_requested_stop'
            Save-CampaignState
            break
        }
        $taskStateStatus='qualifying'
        Save-CampaignState
        if (-not $taskRecord.episode_id) { throw 'Recorder exited without an episode identity' }
        $taskQualificationLog = Join-Path $taskRunInfo.RunDirectory 'qualification.json'
        $env:PYTHONPATH = Join-Path $taskRoot 'src'
        $env:PYTHONIOENCODING = 'utf-8'
        & (Join-Path $taskRoot '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'qualify_live_episode.py') (Split-Path -Parent $taskManifestPath) > $taskQualificationLog
        if ($LASTEXITCODE -ne 0) { $taskRecord.status='technical_qualification_failed'; throw 'Live quality gate stopped the campaign; preserved episode for review' }
        if (-not $taskCompletion) { $taskRecord.status='routes_incomplete_preserved'; throw 'Native routes did not finish within the recording bound; no automatic replay' }
        $taskRecord.status='technical_qualified_review_pending'
        $taskRecord.qualification_log=$taskQualificationLog
        Save-CampaignState
    }
    if ($taskStateStatus -ne 'bounded_or_requested_stop') { $taskStateStatus='queue_finished_review_pending' }
    Save-CampaignState
} catch {
    $taskResults += @{ stage=$taskStage; status='failed'; error=$_.Exception.Message; verified_success=$false }
    $taskStateStatus='failed_preserved'; Save-CampaignState
    if ($taskRunInfo -and $taskRunInfo.StopFile) { New-Item -ItemType File -Path $taskRunInfo.StopFile -Force | Out-Null }
    if ($taskOwnsBGI) { try { Close-OwnedBGI } catch { Write-Warning ('BGI cleanup preserved the instance: '+$_.Exception.Message) } }
    throw
}
Write-Host "Campaign report: $taskReportPath"

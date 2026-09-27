param([switch]$Elevated)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskOutput = Join-Path $taskRoot 'reports\local\machine_worker'
New-Item -ItemType Directory -Path $taskOutput -Force | Out-Null
$taskIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$taskIsAdmin = ([Security.Principal.WindowsPrincipal]$taskIdentity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $taskIsAdmin) {
    if ($Elevated) { throw 'Ordinary Windows installation authorization is required' }
    $taskShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    try {
        Start-Process -FilePath $taskShell -Verb RunAs -WindowStyle Hidden -ArgumentList @('-NoProfile','-File',('"'+$PSCommandPath+'"'),'-Elevated')
    } catch {
        @{status='not_installed';error=$_.Exception.Message;utc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json |
            Set-Content -LiteralPath (Join-Path $taskOutput 'install_attempt.json') -Encoding UTF8
        throw
    }
    exit
}
$taskName = 'Stellaria Synaeris Machine Collection'
$taskProbeName = 'Stellaria Synaeris Machine Probe'
$taskExisting = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($taskExisting -and $taskExisting.State -eq 'Running') { throw 'Existing collection preserved; stop it through the collector first' }
$taskInstallRoot = Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)) 'Stellaria Synaeris'
$taskPreviousRequests = @()
$taskCarriedAttempts = @()
$taskPreviousInstallationPath = Join-Path $taskOutput 'installation.json'
if (Test-Path -LiteralPath $taskPreviousInstallationPath) {
    $taskPreviousInstallation = Get-Content -LiteralPath $taskPreviousInstallationPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($taskPreviousInstallation.status -eq 'installed') {
        $taskPreviousRuntime = [IO.Path]::GetFullPath([string]$taskPreviousInstallation.runtime)
        $taskProtectedRoot = [IO.Path]::GetFullPath($taskInstallRoot).TrimEnd('\') + '\'
        if (-not $taskPreviousRuntime.StartsWith($taskProtectedRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Previous installed runtime escaped the protected application directory'
        }
        $taskPreviousHistoryPath = Join-Path $taskPreviousRuntime 'machine-history.json'
        if (Test-Path -LiteralPath $taskPreviousHistoryPath) {
            $taskPreviousHistory = Get-Content -LiteralPath $taskPreviousHistoryPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $taskPreviousRequests = @($taskPreviousHistory.requests)
            $taskCarriedAttempts = @($taskPreviousHistory.attempted_hashes)
        }
    }
}
$taskArchivedRequests = @()
foreach ($taskRequestFile in @(Get-ChildItem -LiteralPath $taskOutput -Filter '*.request.json' -File)) {
    $taskArchivedRequest = Get-Content -LiteralPath $taskRequestFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskArchivedRequests += [string]$taskArchivedRequest.request_id
}
$taskPreviousRequests = @($taskPreviousRequests) + @($taskArchivedRequests) | Sort-Object -Unique
$taskRuntime = Join-Path $taskInstallRoot ('runtime-'+[guid]::NewGuid().ToString('N'))
$taskAppSource = Join-Path $taskRoot 'dist\SynaerisCollector'
$taskBGISource = Join-Path $taskRoot '.external\bettergi-portable'
$taskQualification = Get-Content -LiteralPath (Join-Path $taskRoot 'reports\local\bgi-qualified.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($taskQualification.status -ne 'build_passed') { throw 'BGI build qualification missing' }
foreach ($taskBinary in @('BetterGI.exe','BetterGI.dll','BetterGI.deps.json','BetterGI.runtimeconfig.json','Fischless.WindowsInput.dll')) {
    if ((Get-FileHash -LiteralPath (Join-Path $taskBGISource $taskBinary) -Algorithm SHA256).Hash -ne $taskQualification.files.$taskBinary) { throw "BGI artifact changed: $taskBinary" }
}
# Program Files inherits the normal Windows protected application-directory ACL.
# No security policy, credentials, autostart trigger, or generic shell endpoint.
New-Item -ItemType Directory -Path $taskRuntime -Force | Out-Null
Copy-Item -LiteralPath $taskAppSource -Destination (Join-Path $taskRuntime 'Collector') -Recurse
Copy-Item -LiteralPath $taskBGISource -Destination (Join-Path $taskRuntime 'BetterGI') -Recurse
$taskAppDirectory = Join-Path $taskRuntime 'Collector'
$taskExecutable = Join-Path $taskAppDirectory 'SynaerisCollector.exe'
$taskBGIDirectory = Join-Path $taskRuntime 'BetterGI'
$taskBinaryHashes = @{}
foreach ($taskFile in @(Get-ChildItem -LiteralPath $taskBGIDirectory -Recurse -File | Where-Object Extension -in @('.exe','.dll'))) {
    $taskRelative = $taskFile.FullName.Substring($taskBGIDirectory.Length+1).Replace('\','/')
    $taskBinaryHashes[$taskRelative] = (Get-FileHash -LiteralPath $taskFile.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
}
$taskPreviousAttempts = Get-Content -LiteralPath (Join-Path $taskOutput 'previous_attempts.json') -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($taskRequestId in $taskPreviousRequests) { if ($taskRequestId -notmatch '^[0-9a-f]{32}$') { throw 'Invalid previous request identity' } }
$taskAllAttemptedHashes = @($taskPreviousAttempts.hashes) + @($taskCarriedAttempts) | Sort-Object -Unique
foreach ($taskHash in $taskAllAttemptedHashes) { if ($taskHash -notmatch '^[0-9a-f]{64}$') { throw 'Invalid previous-attempt asset identity' } }
$taskPolicy = @{ schema_version=1; workspace=$taskRoot; source_bgi=$taskBGISource; bgi=$taskBGIDirectory;
    bgi_binary_hashes=$taskBinaryHashes; previously_attempted_hashes=@($taskAllAttemptedHashes); session_minutes=120;
    scope='typed_native_navigation_pickup_and_combat_pathing'; installed_utc=[DateTime]::UtcNow.ToString('o') }
$taskPolicy | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $taskAppDirectory 'machine-policy.json') -Encoding UTF8
@{requests=@($taskPreviousRequests | Sort-Object -Unique); attempted_hashes=@($taskAllAttemptedHashes)} |
    ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $taskAppDirectory 'machine-history.json') -Encoding UTF8
@{hashes=@($taskAllAttemptedHashes)} | ConvertTo-Json -Depth 4 |
    Set-Content -LiteralPath (Join-Path $taskOutput 'previous_attempts.json') -Encoding UTF8
& (Join-Path $env:SystemRoot 'System32\icacls.exe') $taskRuntime '/setowner' '*S-1-5-32-544' '/T' '/C' '/Q' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Protected application ownership could not be established' }
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $taskIdentity.User.Value -LogonType Interactive -RunLevel Highest
$taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$taskAction = New-ScheduledTaskAction -Execute $taskExecutable -Argument '--machine-worker' -WorkingDirectory $taskAppDirectory
$taskProbeAction = New-ScheduledTaskAction -Execute $taskExecutable -Argument '--machine-probe' -WorkingDirectory $taskAppDirectory
Register-ScheduledTask -TaskName $taskName -Action $taskAction -Principal $taskPrincipal -Settings $taskSettings -Description 'User-authorized bounded Genshin dataset collection; typed native routes only, safe stop and audit files' -Force | Out-Null
Register-ScheduledTask -TaskName $taskProbeName -Action $taskProbeAction -Principal $taskPrincipal -Settings $taskSettings -Description 'Synaeris installation integrity probe; no gameplay or input' -Force | Out-Null
$taskScheduler = New-Object -ComObject 'Schedule.Service'
$taskScheduler.Connect()
$taskFolder = $taskScheduler.GetFolder('\')
# Delegate read/execute for these two fixed tasks; modification remains admin-only.
$taskDescriptor = 'O:BAG:BAD:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGX;;;'+$taskIdentity.User.Value+')'
$taskFolder.GetTask($taskName).SetSecurityDescriptor($taskDescriptor,0)
$taskFolder.GetTask($taskProbeName).SetSecurityDescriptor($taskDescriptor,0)
@{status='installed'; task_name=$taskName; probe_task=$taskProbeName; runtime=$taskAppDirectory;
    executable=$taskExecutable; executable_sha256=(Get-FileHash -LiteralPath $taskExecutable -Algorithm SHA256).Hash;
    user_sid=$taskIdentity.User.Value;utc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json |
    Set-Content -LiteralPath (Join-Path $taskOutput 'installation.json') -Encoding UTF8
@{status='installed';runtime=$taskAppDirectory;utc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json |
    Set-Content -LiteralPath (Join-Path $taskOutput 'install_attempt.json') -Encoding UTF8
Start-ScheduledTask -TaskName $taskProbeName

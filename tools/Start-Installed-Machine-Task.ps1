$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskInstallation = Get-Content -LiteralPath (Join-Path $taskRoot 'reports\local\machine_worker\installation.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($taskInstallation.status -ne 'installed') { throw 'Install the ordinarily authorized app task first' }
$taskName = 'Stellaria Synaeris Machine Collection'
$taskDefinition = Get-ScheduledTask -TaskName $taskName
if ($taskDefinition.State -eq 'Running') { Write-Host 'The existing collection is still running'; exit }
if ($taskDefinition.Actions.Count -ne 1 -or $taskDefinition.Actions[0].Execute -ne $taskInstallation.executable -or $taskDefinition.Actions[0].Arguments -ne '--machine-worker') { throw 'Installed collection task changed' }
if ((Get-FileHash -LiteralPath $taskInstallation.executable -Algorithm SHA256).Hash -ne $taskInstallation.executable_sha256) { throw 'Installed executable changed' }
Start-ScheduledTask -TaskName $taskName
Write-Host 'User-authorized collection task dispatched'

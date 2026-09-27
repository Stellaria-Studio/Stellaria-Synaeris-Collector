$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskErrors = @()
foreach ($taskFile in @('Start-Live-Collection.ps1','Start-BGI-Telemetry.ps1',
    'tools\Run-Machine-Campaign.ps1','tools\Launch-Elevated-Collection.ps1',
    'tools\Install-Machine-Task.ps1','tools\Start-Installed-Machine-Task.ps1',
    'tools\Package-Human-Collector.ps1')) {
    $taskTokens = $null
    $taskParseErrors = $null
    [Management.Automation.Language.Parser]::ParseFile((Join-Path $taskRoot $taskFile),
        [ref]$taskTokens,[ref]$taskParseErrors) | Out-Null
    $taskErrors += $taskParseErrors
}
if ($taskErrors.Count) { $taskErrors | Format-List; exit 1 }
Write-Host ('Launch scripts passed PowerShell '+$PSVersionTable.PSVersion.ToString()+' syntax checks')

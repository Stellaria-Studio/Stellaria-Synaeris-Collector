$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
$taskCheckout = Join-Path $taskRoot '.external\bettergi'
if (-not (Test-Path -LiteralPath $taskCheckout)) {
    git clone https://github.com/babalae/better-genshin-impact.git $taskCheckout
    if ($LASTEXITCODE -ne 0) { throw 'BetterGI clone failed' }
    git -C $taskCheckout checkout 42e1c0e745670eb4443c1e0357fba963eb24dfcd
    if ($LASTEXITCODE -ne 0) { throw 'BetterGI revision unavailable' }
}
if (-not (Test-Path -LiteralPath (Join-Path $taskCheckout 'BetterGenshinImpact\Core\Telemetry\SynaerisTelemetry.cs'))) {
    & '.\.venv\Scripts\python.exe' tools\patch_bgi.py $taskCheckout
    if ($LASTEXITCODE -ne 0) { throw 'Telemetry patch failed' }
}
else {
    & '.\.venv\Scripts\python.exe' tools\patch_bgi.py $taskCheckout --upgrade-existing
    if ($LASTEXITCODE -ne 0) { throw 'Telemetry hook update failed' }
}
dotnet build (Join-Path $taskCheckout 'BetterGenshinImpact.sln') -c Release -p:Platform=x64
if ($LASTEXITCODE -ne 0) { throw 'BetterGI telemetry build failed' }
$taskBinaryDirectory = Join-Path $taskCheckout 'BetterGenshinImpact\bin\x64\Release\net8.0-windows10.0.22621.0'
$taskQualifiedDirectory = Join-Path $taskRoot 'reports\local'
New-Item -ItemType Directory -Path $taskQualifiedDirectory -Force | Out-Null
$taskHashes = @{}
foreach ($taskBinary in @('BetterGI.exe','BetterGI.dll','BetterGI.deps.json','BetterGI.runtimeconfig.json','Fischless.WindowsInput.dll')) {
    $taskHashes[$taskBinary] = (Get-FileHash -LiteralPath (Join-Path $taskBinaryDirectory $taskBinary) -Algorithm SHA256).Hash
}
$taskPortable = Join-Path $taskRoot '.external\bettergi-portable'
if (Test-Path -LiteralPath $taskPortable) {
    # Update only qualified program artifacts. Preserve the portable User tree,
    # which contains local routes, strategies and collection groups.
    foreach ($taskBinary in $taskHashes.Keys) {
        Copy-Item -LiteralPath (Join-Path $taskBinaryDirectory $taskBinary) -Destination (Join-Path $taskPortable $taskBinary) -Force
    }
}
@{ status='build_passed'; real_input_observer_qualified=$false; files=$taskHashes; built_utc=[DateTime]::UtcNow.ToString('o') } |
    ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $taskQualifiedDirectory 'bgi-qualified.json') -Encoding UTF8

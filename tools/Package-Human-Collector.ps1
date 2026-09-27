param([string]$Version = '0.6.0')
$ErrorActionPreference = 'Stop'
$collectorRoot = Split-Path -Parent $PSScriptRoot
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid package version' }
$collectorVersionSource = Get-Content -LiteralPath (Join-Path $collectorRoot 'src/synaeris_collector/version.py') -Raw
if ($collectorVersionSource -notmatch ('APP_VERSION = "'+[regex]::Escape($Version)+'"')) { throw 'Package version differs from executable version' }
$collectorSource = Join-Path $collectorRoot 'dist/SynaerisCollector'
$collectorRelease = Join-Path $collectorRoot ('dist/releases/SynaerisCollector-Human-'+$Version)
$collectorZip = $collectorRelease + '.zip'
if ((Test-Path -LiteralPath $collectorRelease) -or (Test-Path -LiteralPath $collectorZip)) { throw 'Existing release preserved; choose a new version' }
New-Item -ItemType Directory -Path (Split-Path -Parent $collectorRelease) -Force | Out-Null
Copy-Item -LiteralPath $collectorSource -Destination $collectorRelease -Recurse
Copy-Item -LiteralPath (Join-Path $collectorRoot 'docs/HUMAN_COLLECTOR.md') -Destination (Join-Path $collectorRelease 'READ_ME.md')
$collectorExecutableHash = (Get-FileHash -LiteralPath (Join-Path $collectorRelease 'SynaerisCollector.exe')).Hash.ToLowerInvariant()
@{schema='synaeris-collector-release-v1';version=$Version;
  repository='Stellaria-Studio/Stellaria-Synaeris-Collector';
  executable_sha256=$collectorExecutableHash} | ConvertTo-Json |
    Set-Content -LiteralPath (Join-Path $collectorRelease 'collector-release.json') -Encoding UTF8
# Include one containing directory so extracting does not scatter runtime files.
Compress-Archive -LiteralPath $collectorRelease -DestinationPath $collectorZip -CompressionLevel Optimal
@{version=$Version;software_directory=$collectorRelease;archive=$collectorZip;
  archive_sha256=(Get-FileHash -LiteralPath $collectorZip).Hash;
  executable_sha256=$collectorExecutableHash} |
    ConvertTo-Json | Set-Content -LiteralPath ($collectorRelease+'.manifest.json') -Encoding UTF8
Write-Output $collectorZip

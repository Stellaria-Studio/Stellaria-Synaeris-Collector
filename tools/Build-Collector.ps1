param([string]$Python = '')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
if (-not $Python) {
    if (Test-Path -LiteralPath '.packenv\Scripts\python.exe') { $Python = '.\.packenv\Scripts\python.exe' }
    else { $Python = '.\.venv\Scripts\python.exe' }
}
& $Python -m PyInstaller --noconfirm --clean --onedir --windowed --uac-admin --name SynaerisCollector --paths src --add-data "src/synaeris_collector/bgi_native_defaults.json;synaeris_collector" --collect-all rapidocr --collect-all imageio_ffmpeg --collect-all dxcam --collect-all windows_capture --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 --exclude-module torch --exclude-module tensorflow --exclude-module scipy --exclude-module matplotlib --exclude-module pandas --exclude-module IPython --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PySide2 --exclude-module PySide6 collector_app.py
if ($LASTEXITCODE -ne 0) { throw 'Collector packaging failed' }

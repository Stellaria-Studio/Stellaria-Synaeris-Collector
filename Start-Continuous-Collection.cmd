@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -File "%~dp0tools\Start-Installed-Machine-Task.ps1"

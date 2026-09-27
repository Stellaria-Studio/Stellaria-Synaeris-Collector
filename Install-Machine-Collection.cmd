@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -File "%~dp0tools\Install-Machine-Task.ps1"

@echo off
powershell.exe -NoProfile -File "%~dp0tools\Launch-Elevated-Collection.ps1" -Remaining
if errorlevel 1 pause

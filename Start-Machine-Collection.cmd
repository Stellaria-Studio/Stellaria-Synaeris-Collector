@echo off
powershell.exe -NoProfile -File "%~dp0tools\Launch-Elevated-Collection.ps1"
if errorlevel 1 pause

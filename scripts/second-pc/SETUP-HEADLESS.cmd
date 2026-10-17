@echo off
rem Double-click me on DESKTOP-5BEGSB5. Runs Setup-HeadlessHost.ps1 (it asks for administrator rights itself).
rem Extra switches pass through, e.g.  SETUP-HEADLESS.cmd -CheckOnly   or   SETUP-HEADLESS.cmd -WhatIf
setlocal
set "PS1=%~dp0Setup-HeadlessHost.ps1"
if not exist "%PS1%" set "PS1=%USERPROFILE%\Desktop\Projects\notifications\scripts\second-pc\Setup-HeadlessHost.ps1"
if not exist "%PS1%" (
  echo Could not find Setup-HeadlessHost.ps1 next to this file or in Desktop\Projects\notifications\scripts\second-pc.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
echo.
echo If an administrator window opened, the setup continues there. You can close this one.
pause
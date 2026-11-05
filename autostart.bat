@echo off
REM ---------------------------------------------------------------------------
REM  olt-monitor-router - auto-start at Windows logon
REM
REM    autostart.bat            install the scheduled task and start it now
REM    autostart.bat remove     remove the scheduled task
REM    autostart.bat status     show the task and whether it is running
REM
REM  Under the hood this calls tools\autostart.ps1 (no admin rights needed).
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" set "PS=powershell.exe"

"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\autostart.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" pause
endlocal & exit /b %RC%

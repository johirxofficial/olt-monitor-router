@echo off
REM ---------------------------------------------------------------------------
REM  olt-monitor-router - stop the running monitor
REM
REM    stop.bat            stop whatever listens on port 5002
REM    stop.bat 8080       another port
REM ---------------------------------------------------------------------------
setlocal EnableExtensions
set "PORT=%~1"
if "%PORT%"=="" set "PORT=5002"

set "FOUND="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do call :kill %%p

if not defined FOUND echo [i] nothing is listening on port %PORT%
if defined FOUND (
    echo [*] stopped
    echo [i] if autostart.bat installed a scheduled task, remove it to keep it stopped: autostart.bat remove
)
endlocal
exit /b 0

:kill
echo [*] stopping pid %1 on port %PORT%
taskkill /f /pid %1 >nul 2>&1
if not errorlevel 1 set "FOUND=1"
exit /b 0

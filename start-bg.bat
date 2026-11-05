@echo off
REM ---------------------------------------------------------------------------
REM  olt-monitor-router - start in the background (no console window)
REM
REM    start-bg.bat             start it hidden on port 5002
REM    start-bg.bat 8080        use another port
REM
REM  The window is hidden because it runs through pythonw.exe, and the process
REM  is detached, so it keeps running after this window closes.  Console output
REM  is copied to logs\console.out / logs\console.err, and the rotating log is
REM  logs\olt_monitor.log.
REM
REM  Stop it with stop.bat, check it with status.bat.
REM  To have it come back by itself after a reboot: autostart.bat
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=5002"

REM ---- pick an interpreter; the *w* variants open no console at all --------
set "PY="
set "ARGV=-3.12"
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    set "PY=%~dp0.venv\Scripts\pythonw.exe"
    set "ARGV="
) else (
    for /f "delims=" %%p in ('where pyw 2^>nul') do if not defined PY set "PY=%%p"
    if not defined PY for /f "delims=" %%p in ('where pythonw 2^>nul') do if not defined PY set "PY=%%p"
)
if not defined PY (
    echo [!] pythonw.exe was not found - falling back to python with a hidden window
    if exist "%~dp0.venv\Scripts\python.exe" (
        set "PY=%~dp0.venv\Scripts\python.exe"
        set "ARGV="
    ) else (
        for /f "delims=" %%p in ('where py 2^>nul') do if not defined PY set "PY=%%p"
        if not defined PY for /f "delims=" %%p in ('where python 2^>nul') do if not defined PY set "PY=%%p"
    )
)
if not defined PY (
    echo [X] Python 3.10+ was not found on PATH.
    pause
    exit /b 1
)

if not exist "logs" mkdir "logs"

if not exist ".env" (
    echo [X] .env is missing - run start.bat once and set your credentials.
    pause
    exit /b 1
)

REM ---- already running? then do nothing ------------------------------------
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do (
    echo [i] port %PORT% is already in use by pid %%p - nothing to do
    echo [i] dashboard: http://localhost:%PORT%
    goto :end
)

echo [*] starting hidden on port %PORT% ...
powershell -NoProfile -Command ^
  "Start-Process -FilePath '%PY%' -ArgumentList '%ARGV% run.py --port %PORT%' -WorkingDirectory '%CD%' -WindowStyle Hidden -RedirectStandardOutput 'logs\console.out' -RedirectStandardError 'logs\console.err'"
if errorlevel 1 goto :error

echo [*] waiting for http://127.0.0.1:%PORT%/api/health ...
powershell -NoProfile -Command ^
  "1..45 | %% { try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 'http://127.0.0.1:%PORT%/api/health' | Out-Null; Write-Host '    dashboard is up'; exit 0 } catch { Start-Sleep -Milliseconds 700 } }; Write-Host '    still starting - see logs\console.err'; exit 0"

echo [*] dashboard: http://localhost:%PORT%
echo [*] logs:      logs\olt_monitor.log   (stop.bat stops it)
goto :end

:error
echo [X] could not start - see logs\console.err
pause
exit /b 1

:end
endlocal

@echo off
REM ---------------------------------------------------------------------------
REM  olt-monitor-router - Windows start
REM
REM    start.bat            start the monitor (dashboard + poller)
REM    start.bat check      only test OLT SNMP / RouterOS connectivity, then exit
REM    start.bat once       poll every device once, store, and exit
REM    start.bat --port 8080   anything after start.bat is passed to run.py
REM
REM  It uses the .venv next to this file when there is one, otherwise the
REM  system Python (py -3.12, then python).  See start-bg.bat for a hidden
REM  window and stop.bat to stop it.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

REM ---- find an interpreter -----------------------------------------------
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY where py >nul 2>&1 && set "PY=py -3.12"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
    echo [X] Python 3.10+ was not found on PATH.
    echo     Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

REM ---- .env ---------------------------------------------------------------
if not exist ".env" (
    if exist ".env.example" (
        echo [!] .env not found - copying .env.example
        copy /y ".env.example" ".env" >nul
        echo [!] edit .env and set your OLT community / router password, then run start.bat again.
        pause
        exit /b 1
    )
)

REM ---- optional venv bootstrap -------------------------------------------
if /i "%~1"=="venv" (
    echo [*] creating .venv and installing dependencies...
    python -m venv .venv || goto :error
    call .venv\Scripts\python.exe -m pip install --upgrade pip
    call .venv\Scripts\python.exe -m pip install -r requirements.txt || goto :error
    echo [*] .venv is ready - run start.bat
    goto :end
)

REM ---- run ----------------------------------------------------------------
if /i "%~1"=="check" (
    echo [*] testing device connectivity, this can take a while...
    %PY% run.py --check
    goto :end
)

echo [*] interpreter: %PY%
echo [*] starting olt-monitor-router ...
echo [*] dashboard:  http://localhost:5002   (port from config/config.yaml)
echo [*] press Ctrl+C to stop
echo.
%PY% run.py %*
goto :end

:error
echo [X] something went wrong - see the messages above.
pause
exit /b 1

:end
endlocal

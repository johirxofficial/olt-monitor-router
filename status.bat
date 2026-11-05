@echo off
REM ---------------------------------------------------------------------------
REM  olt-monitor-router - is it running, and is it healthy?
REM
REM    status.bat          show the port, the process and the /api/health answer
REM    status.bat 8080     another port
REM ---------------------------------------------------------------------------
setlocal EnableExtensions
cd /d "%~dp0"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=5002"

echo === port %PORT% ==============================================
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" || echo   nothing is listening on port %PORT%
echo.
echo === processes =================================================
tasklist /fi "imagename eq python.exe" /fo table | findstr /i "python" || echo   no python.exe running
tasklist /fi "imagename eq pythonw.exe" /fo table | findstr /i "python" || echo   no pythonw.exe running
echo.
echo === /api/health ==============================================
powershell -NoProfile -Command ^
  "try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 'http://127.0.0.1:%PORT%/api/health').Content } catch { Write-Host '  no answer - the service is down or still starting' }"
echo.
echo === auto-start ===============================================
powershell -NoProfile -Command ^
  "$t = Get-ScheduledTask -TaskName 'olt-monitor-router' -ErrorAction SilentlyContinue; if ($t) { Write-Host ('  task olt-monitor-router: ' + $t.State) } else { Write-Host '  no auto-start task installed' }"
echo.
echo === last log lines ===========================================
if exist "logs\olt_monitor.log" (
    powershell -NoProfile -Command "Get-Content 'logs\olt_monitor.log' -Tail 10"
) else (
    echo   logs\olt_monitor.log does not exist yet
)
endlocal

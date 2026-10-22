@echo off
cd /d "%~dp0"

if not exist "logs\campus_login.log" (
    echo No log file yet. Run run.bat first.
    pause
    exit /b 1
)

echo ============================================
echo   Last 20 lines of logs\campus_login.log
echo ============================================
echo.
powershell -NoProfile -Command "Get-Content -LiteralPath 'logs\campus_login.log' -Tail 20 -Encoding UTF8"
echo.
echo --------------------------------------------
echo   Login success / already online = OK
echo   Network unreachable            = WiFi not connected
echo   Login failed                   = account / password / request
echo --------------------------------------------
pause

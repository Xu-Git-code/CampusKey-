@echo off
cd /d "%~dp0"
echo ============================================
echo  Campus auto-login  -  run once
echo ============================================
echo.

python --version >nul 2>nul
if errorlevel 1 (
    py --version >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Python not found. Install Python 3.8+ first.
        pause
        exit /b 1
    )
    py campus_login.py
) else (
    python campus_login.py
)

echo.
echo --------------------------------------------
echo Exit code: %errorlevel%
echo   0 = ok / already online
echo   10 = config problem      11 = network down
echo   12 = request failed      13 = login rejected
echo Full log: logs\campus_login.log
echo --------------------------------------------
pause

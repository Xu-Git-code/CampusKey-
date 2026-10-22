@echo off
cd /d "%~dp0"
echo ============================================
echo  Campus auto-login  -  dry run
echo  (parse config only, nothing is sent)
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
    py campus_login.py --dry-run
) else (
    python campus_login.py --dry-run
)

echo.
echo Compare the output above with F12 -^> Network -^> your login request.
echo If they match, run run.bat to login for real.
echo.
pause

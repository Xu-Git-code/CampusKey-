@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  Campus auto-login  -  build exe
echo ============================================
echo.

python --version >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found or not working.
    echo Install Python 3.8+ from https://www.python.org/downloads/
    echo and remember to check "Add python.exe to PATH".
    pause
    exit /b 1
)

echo [1/4] Installing requests and pyinstaller ...
python -m pip install --upgrade pip >nul 2>nul
python -m pip install --upgrade requests pyinstaller
if errorlevel 1 goto :fail

echo.
echo [2/4] Building CampusLogin.exe  (console, for testing) ...
python -m PyInstaller --noconfirm --clean --onefile --console --name CampusLogin campus_login.py
if errorlevel 1 goto :fail

echo.
echo [3/4] Building CampusLoginSilent.exe  (no window, for autostart) ...
python -m PyInstaller --noconfirm --onefile --noconsole --name CampusLoginSilent campus_login.py
if errorlevel 1 goto :fail

echo.
echo [4/4] Copying config files to dist ...
copy /y "config.ini" "dist\config.ini" >nul
copy /y "login_request.txt" "dist\login_request.txt" >nul

echo.
echo ============================================
echo  Done. Output folder: dist
echo.
echo    dist\CampusLogin.exe        console version - test with this first
echo    dist\CampusLoginSilent.exe  silent version  - use for autostart
echo    dist\config.ini             your account / settings
echo    dist\login_request.txt      your captured login request
echo ============================================
pause
exit /b 0

:fail
echo.
echo [ERROR] Build failed. Read the message above.
echo Tips: check your network, or try:
echo    python -m pip install requests pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
pause
exit /b 1

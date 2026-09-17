@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo ============================================
echo  Step 1: Install libraries for web app
echo  (one-time, takes 3-5 minutes)
echo ============================================
echo.

python --version > nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed.
    echo Download from: https://www.python.org/downloads/
    pause
    exit /b 1
)

python --version
echo.

echo Upgrading pip...
python -m pip install --upgrade pip --quiet

echo.
echo Installing required libraries from requirements.txt ...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Library install failed.
    echo Check internet connection or run as administrator.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Step 1 done. Next: run 2_run.bat
echo  (file is named "2_실행하기.bat")
echo ============================================
echo.
pause

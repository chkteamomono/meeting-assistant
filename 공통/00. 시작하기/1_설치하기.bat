@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo ============================================
echo  업무 보조 비서 — 전체 설치
echo  (런처 + 회의 보조 비서 + 면접 보조 비서, 한 번에)
echo  (최초 1회, 5~10분쯤 걸릴 수 있음)
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
echo [1/3] 런처 라이브러리 설치...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] 런처 라이브러리 설치 실패.
    pause
    exit /b 1
)

echo.
echo [2/3] 회의 보조 비서 라이브러리 설치...
python -m pip install -r "..\02. 회의 보조 비서 실행하기\requirements.txt"
if errorlevel 1 (
    echo.
    echo [ERROR] 회의 보조 비서 라이브러리 설치 실패.
    pause
    exit /b 1
)

echo.
echo [3/3] 면접 보조 비서 라이브러리 설치...
python -m pip install -r "..\면접\02. 면접 보조 비서 실행하기\requirements.txt"
if errorlevel 1 (
    echo.
    echo [ERROR] 면접 보조 비서 라이브러리 설치 실패.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  설치 완료. 다음: 2_실행하기.bat
echo ============================================
echo.
pause

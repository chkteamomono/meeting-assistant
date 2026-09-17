@echo off
chcp 65001 > nul
cd /d "%~dp0소스코드\00. 시작하기"

python --version > nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python이 설치되어 있지 않습니다.
    echo https://www.python.org/downloads/ 에서 설치한 뒤 다시 실행하세요.
    pause
    exit /b 1
)

echo 필요한 프로그램을 확인하는 중입니다... (처음 실행이면 몇 분 걸릴 수 있습니다)
python -m pip install --upgrade pip --quiet

python -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo.
    echo [ERROR] 런처 라이브러리 설치 실패. 인터넷 연결을 확인하세요.
    pause
    exit /b 1
)

python -m pip install -r "..\02. 회의 보조 비서 실행하기\requirements.txt" --quiet
if errorlevel 1 (
    echo.
    echo [ERROR] 회의 보조 비서 라이브러리 설치 실패. 인터넷 연결을 확인하세요.
    pause
    exit /b 1
)

python -m pip install -r "..\02. 면접 보조 비서 실행하기\requirements.txt" --quiet
if errorlevel 1 (
    echo.
    echo [ERROR] 면접 보조 비서 라이브러리 설치 실패. 인터넷 연결을 확인하세요.
    pause
    exit /b 1
)

REM pythonw.exe(콘솔 없는 파이썬)로 백그라운드에서 띄운다 — 검은 창이 남지 않는다.
REM 이 cmd 창 자체는 start로 띄우자마자 바로 닫힌다.
start "" pythonw.exe "서버.py"

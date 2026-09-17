@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo ============================================
echo  기록 보조 비서 — 설치
echo ============================================
echo.

if not exist "소스코드.zip" (
    echo [ERROR] 소스코드.zip을 찾을 수 없습니다. 이 설치.bat과 같은 폴더에 있어야 합니다.
    pause
    exit /b 1
)

python --version > nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python이 설치되어 있지 않습니다.
    echo https://www.python.org/downloads/ 에서 설치한 뒤 다시 실행하세요.
    pause
    exit /b 1
)

set "설치위치=%~dp0기록 보조 비서"

if exist "%설치위치%" (
    echo [ERROR] "%설치위치%" 폴더가 이미 있습니다 — 기존 설치를 덮어쓰지 않도록 멈춥니다.
    echo 다시 설치하려면 그 폴더를 지우거나, 이 설치.bat을 다른 폴더로 옮긴 뒤 다시 실행하세요.
    pause
    exit /b 1
)

echo 소스코드를 푸는 중입니다...
python "압축풀기.py" "소스코드.zip" "%설치위치%"
if errorlevel 1 (
    echo.
    echo [ERROR] 압축 풀기 실패.
    pause
    exit /b 1
)

echo.
echo 기본 폴더트리를 만드는 중입니다...
python "%설치위치%\소스코드\00. 시작하기\폴더_초기설정.py"

echo.
echo ============================================
echo  설치 완료!
echo  "%설치위치%" 안의 실행하기.bat 을 눌러 시작하세요.
echo ============================================
echo.
pause

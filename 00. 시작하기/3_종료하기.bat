@echo off
chcp 65001 > nul
cd /d "%~dp0"

if not exist "실행중.pid" (
    echo 실행 중인 것으로 보이는 기록이 없습니다. 이미 종료됐을 수 있습니다.
    pause
    exit /b
)

set /p PID=<실행중.pid
taskkill /F /PID %PID% >nul 2>&1
del "실행중.pid" >nul 2>&1
echo 런처를 종료했습니다. (회의·면접 보조 비서가 켜져 있다면 각자 따로 꺼야 합니다)
pause

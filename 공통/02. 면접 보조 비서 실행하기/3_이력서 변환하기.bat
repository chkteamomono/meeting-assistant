@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo ============================================
echo  스캔 이력서 PDF를 텍스트로 변환합니다
echo  (면접 전에 한 번만 실행하세요)
echo ============================================
echo.
echo  글자가 들어 있는 PDF는 건너뜁니다.
echo  스캔해서 만든 PDF만 Claude가 읽어 .txt로 저장합니다.
echo  쪽수가 많으면 몇 분 걸릴 수 있습니다.
echo.

python "이력서변환.py"

if errorlevel 1 (
    echo.
    echo [ERROR] Script failed. Check messages above.
    pause
)

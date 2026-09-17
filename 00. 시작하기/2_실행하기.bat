@echo off
chcp 65001 > nul
cd /d "%~dp0"
REM pythonw.exe(콘솔 없는 파이썬)로 백그라운드에서 띄운다 — 검은 창이 남지 않는다.
REM 이 cmd 창 자체는 start로 띄우자마자 바로 닫힌다 — 뜨더라도 아주 잠깐이다.
REM 종료는 3_종료하기.bat 를 쓰세요. (회의·면접 보조 비서는 각자 탭을 닫으면 자동 종료됩니다)
start "" pythonw.exe "서버.py"

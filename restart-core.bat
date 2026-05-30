@echo off
echo [ReCoder] 1) 옛 Core 프로세스 강제 종료...
taskkill /F /IM recoder-core.exe >nul 2>&1
echo [ReCoder] 2) 싱글톤 상태 완전 초기화 (lock + runtime)...
del "%USERPROFILE%\.recoder\core.lock" >nul 2>&1
del "%USERPROFILE%\.recoder\runtime.json" >nul 2>&1
echo [ReCoder] 3) venv 소스 Core 시작. 이 창은 켜둔 채로 두세요.
echo            ('Application startup complete' 가 보이면 성공.
echo             VSCode 사이드바에서 지도를 누르면 여기에 POST /api/map 로그가 찍힙니다.)
echo.
cd /d C:\ReCoder\Re-Coder\core
".venv\Scripts\python.exe" main.py
echo.
echo [ReCoder] Core 가 종료되었습니다.
pause

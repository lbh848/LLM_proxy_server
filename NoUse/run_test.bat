@echo off
chcp 65001 >nul
echo ========================================
echo   LLM 프록시 서버 연결 테스트
echo ========================================
echo.

REM 가상환경 활성화
call venv\Scripts\activate.bat

REM 테스트 실행
python test_connection.py
pause

@echo off
chcp 65001 >nul
echo ========================================
echo   LLM 프록시 서버 실행
echo ========================================
echo.

REM 가상환경 활성화
call venv\Scripts\activate.bat

REM 서버 실행
python proxy_server.py

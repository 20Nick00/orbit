@echo off
setlocal
cd /d "%~dp0"
if not defined PYTHON set "PYTHON=%~dp0.venv\Scripts\python.exe"
if "%~1"=="demo" goto demo
if "%~1"=="demo-10k" goto demo10k
if "%~1"=="run" goto run
if "%~1"=="test" goto test
echo Usage: .\make demo ^| demo-10k ^| run ^| test
exit /b 1

:demo
"%PYTHON%" -m alembic upgrade head
if errorlevel 1 exit /b %errorlevel%
"%PYTHON%" -m core.demo
exit /b %errorlevel%

:demo10k
"%PYTHON%" -m alembic upgrade head
if errorlevel 1 exit /b %errorlevel%
"%PYTHON%" -m core.demo --transactions 10000
exit /b %errorlevel%

:run
"%PYTHON%" -m alembic upgrade head
if errorlevel 1 exit /b %errorlevel%
"%PYTHON%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
exit /b %errorlevel%

:test
"%PYTHON%" -m pytest
exit /b %errorlevel%

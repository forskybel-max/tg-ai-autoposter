@echo off
cd /d "%~dp0"
title Telegram AI Autoposter

set "PY_CMD="
where python >nul 2>nul && (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY_CMD=python"
)
if not defined PY_CMD (
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not defined PY_CMD (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
        if exist "%%D\python.exe" set "PY_CMD=%%D\python.exe"
    )
)

if not exist ".env" (
    echo [!] Creating .env from .env.example...
    copy /y ".env.example" ".env" >nul
    echo [!] Fill in your API keys in Notepad, save Ctrl+S, and close Notepad to continue...
    notepad ".env"
)

if not exist ".venv\Scripts\python.exe" (
    if not defined PY_CMD (
        echo [ERROR] Python 3.11+ was not found! Install from https://www.python.org/downloads/
        pause
        exit /b 1
    )
    echo [*] Creating virtual environment (.venv)...
    "%PY_CMD%" -m venv .venv
)

if not exist ".venv\Lib\site-packages\aiogram" (
    echo [*] Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*tg_ai_autoposter*main.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>nul

echo [*] Starting Telegram AI Autoposter...
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" "%~dp0main.py"
echo.
pause

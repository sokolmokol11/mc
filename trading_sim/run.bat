@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python не найден. Установи его с https://www.python.org/downloads/
  echo При установке обязательно поставь галочку "Add python.exe to PATH".
  echo.
  pause
  exit /b
)
python -m pip install -q --disable-pip-version-check -r requirements.txt
python main.py
pause

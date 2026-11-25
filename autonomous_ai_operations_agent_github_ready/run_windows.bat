@echo off
setlocal
cd /d "%~dp0"
if not exist .venv (
  py -3.12 -m venv .venv
  if errorlevel 1 py -3 -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if not exist .env copy .env.example .env
python scripts\seed_demo.py
if errorlevel 1 exit /b 1
echo.
echo Setup complete. Run run_api.bat and run_dashboard.bat in separate terminals.
pause

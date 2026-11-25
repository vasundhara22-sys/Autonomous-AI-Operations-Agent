@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m uvicorn app.api:create_app --factory --host 127.0.0.1 --port 8000

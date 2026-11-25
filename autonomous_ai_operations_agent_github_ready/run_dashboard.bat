@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m streamlit run dashboard\streamlit_app.py

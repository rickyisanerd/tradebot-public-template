@echo off
python -m venv .venv
call .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if not exist .env copy .env.example .env
@echo.
@echo Ready. Run: .venv\Scripts\activate ^&^& python run_tests.py

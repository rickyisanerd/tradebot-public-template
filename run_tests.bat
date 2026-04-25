@echo off
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0run_tests.py"
) else (
  python "%~dp0run_tests.py"
)

@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  python -m venv .venv
)

echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install -e .

echo Starting local panel...
start "" http://127.0.0.1:7860
start "chatgpt-register-panel" /min ".venv\Scripts\python.exe" -m chatgpt_register_sub2api.web

endlocal

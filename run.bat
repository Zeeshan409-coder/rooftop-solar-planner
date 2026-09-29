@echo off
cd /d "%~dp0"
where python >nul 2>nul || (
  echo Python was not found. Install Python 3.11 or newer from python.org and tick "Add python.exe to PATH".
  pause
  exit /b 1
)
if not exist .venv (
  echo Creating virtual environment...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
echo Checking packages (the first time takes a few minutes)...
python -m pip install -q -r requirements.txt || (
  echo Installing packages failed. Check your internet connection and run this again.
  pause
  exit /b 1
)
echo Starting on http://localhost:8000  (close this window to stop)
start "" cmd /c "timeout /t 5 >nul & start http://localhost:8000"
python -m uvicorn server.app:app --port 8000

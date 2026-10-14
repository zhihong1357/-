@echo off
cd /d "%~dp0"
echo Installing required Python packages...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo [ERROR] Python not found or install failed. See README step 1.
) else (
  echo.
  echo Done! Now double-click 2_scan_once.bat
)
pause

@echo off
cd /d "%~dp0"
chcp 65001 >nul
set /p SYM=Symbol (e.g. SOLUSDT): 
python scanner.py --symbol %SYM%
pause

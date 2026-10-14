@echo off
cd /d "%~dp0"
chcp 65001 >nul
python scanner.py --find-chat-id
python scanner.py --test-alert
pause

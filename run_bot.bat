@echo off
cd /d "d:\line-bot-shift-manager"

:: 在原本的指令最後面加上 > bot_log.txt 2>&1
"d:\line-bot-shift-manager\venv\Scripts\uvicorn.exe" main:app --host 127.0.0.1 --port 8000 > "d:\line-bot-shift-manager\bot_log.txt" 2>&1
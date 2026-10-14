@echo off
cd /d D:\munagrocery.in\shopbot
rem JioMart search proxy for the webapp admin panel (port 8085, 0.0.0.0).
rem Starts with every bot launch; exits quietly if already running.
start "" /min run_proxy.cmd
python main.py >> bot_run.log 2>&1

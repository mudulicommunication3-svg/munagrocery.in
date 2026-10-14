@echo off
cd /d D:\munagrocery.in\shopbot
rem Standalone JioMart search proxy for the webapp (port 8085).
rem Also auto-started by run_bot.cmd - safe to launch twice (port guard).
python jiomart_search_proxy.py >> proxy_run.log 2>&1
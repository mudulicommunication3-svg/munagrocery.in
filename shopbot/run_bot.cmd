@echo off
cd /d D:\munagrocery.in\shopbot
rem MUNNA MOBILE & Grocery bot. JioMart search for the webapp runs through
rem the Sheet search bus (search_bus.py -> jiomart_search.fetch_items), the
rem same engine as /product - no local proxy on port 8085 anymore.
python main.py >> bot_run.log 2>&1

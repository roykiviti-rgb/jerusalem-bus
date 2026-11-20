@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python -c "import zoneinfo; zoneinfo.ZoneInfo('Asia/Jerusalem')" 2>NUL || python -m pip install -q tzdata
if not exist data\jerusalem.db python build_data.py --download
python server.py
pause

@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo Downloading the latest timetables from the Ministry of Transport...
python build_data.py --download
pause

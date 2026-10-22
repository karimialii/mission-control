@echo off
cd /d "%~dp0"
where py >nul 2>nul && (py -3 start.py %* & goto :end)
python start.py %*
:end
if errorlevel 1 pause

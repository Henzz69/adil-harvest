@echo off
REM run_bot.bat — start the 24/7 adil bot on Windows (detached)
cd /d "%~dp0"
if not exist state mkdir state
echo Starting adil_bot (rescan + refund + lb + spin + mask + telegram)...
start "" /min pythonw -u adil_bot.py
timeout /t 5 >nul
echo.
echo Log: type adil_bot.log
echo Stop: taskkill /f /im pythonw.exe

@echo off
REM Daily run of the SSC Tender Tracker. Windows Task Scheduler starts this file.
REM Extra options are passed on, e.g.:  run_daily.bat --force
cd /d "%~dp0"
if not exist logs mkdir logs
if not exist ".venv\Scripts\python.exe" (
  echo [%date% %time%] .venv not found - see README.md setup steps >> "logs\scheduler.log"
  exit /b 1
)
echo [%date% %time%] starting >> "logs\scheduler.log"
REM Full details go to logs\run_<date>.log; only crashes (stderr) are added here.
".venv\Scripts\python.exe" -m tender_tracker %* > nul 2>> "logs\scheduler.log"
set RESULT=%ERRORLEVEL%
echo [%date% %time%] finished with code %RESULT% >> "logs\scheduler.log"
exit /b %RESULT%

@echo off
REM Double-click to start a full scan now: websites, new PDFs + OCR, Claude reading, Excel, email.
REM Same as answering "Haan" in the hourly popup. Closing this window stops the scan.
title SSC Tender Scan
cd /d "%~dp0"
if not exist logs mkdir logs
if not exist ".venv\Scripts\python.exe" (
  echo .venv folder nahi mila - README.md ke setup steps dekhein.
  pause
  exit /b 1
)
echo ======================================================================
echo   SSC Tender Scan shuru  -  %date% %time:~0,5%
echo   Sab websites check hongi, lagbhag 30-60 minute lagenge.
echo   Ye window scan khatam hone tak khuli rakhein.
echo ======================================================================
echo.
set PYTHONIOENCODING=utf-8
REM Progress is shown here and saved in the logs folder. Library warnings such as OCR engine notes
REM go to logs\last_scan_warnings.log so this window stays readable.
".venv\Scripts\python.exe" -m tender_tracker --force %* 2> "logs\last_scan_warnings.log"
set RESULT=%ERRORLEVEL%
echo.
echo ======================================================================
if "%RESULT%"=="0" echo   Scan poora hua. Excel: output\MASTER_All_SSC.xlsx
if "%RESULT%"=="2" echo   Scan complete, kuch websites partial/failed hain. Site Status aur logs dekhein.
if "%RESULT%"=="3" echo   Ek scan pehle se chal raha hai. Uske khatam hone ke baad dobara chalayein.
if "%RESULT%"=="4" echo   Internet nahi mila. Internet aane par ye file dobara chalayein.
if not "%RESULT%"=="0" if not "%RESULT%"=="2" if not "%RESULT%"=="3" if not "%RESULT%"=="4" echo   Scan mein dikkat aayi - code %RESULT%. Details: logs folder.
echo ======================================================================
echo.
echo Window band karne ke liye koi bhi key dabayein.
pause > nul
exit /b %RESULT%

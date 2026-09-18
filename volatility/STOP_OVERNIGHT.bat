@echo off
cd /d "%~dp0"
title RITCx Volatility - Stop

echo Stopping the overnight runner and its children...
echo.

REM The runner, the trader and the recorder are three separate processes.
REM Closing the runner's window does not necessarily take the other two with
REM it, and an orphaned trader would keep trading unsupervised - so kill all
REM python here rather than trusting the parent to clean up.
taskkill /F /IM python.exe /T 2>NUL
taskkill /F /IM pythonw.exe /T 2>NUL

echo.
echo Done. Nothing is trading now.
echo Your results.csv and heats\ recordings are untouched.
echo.
pause

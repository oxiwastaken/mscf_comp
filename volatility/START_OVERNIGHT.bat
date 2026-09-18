@echo off
cd /d "%~dp0"
title RITCx Volatility - Overnight Runner
color 0A

echo ============================================================
echo   RITCx VOLATILITY - OVERNIGHT RUNNER
echo ============================================================
echo.
echo   Trades every heat and records it. Leave this window open.
echo.
echo   Before leaving it overnight, check:
echo     - the RIT client is OPEN and connected to the practice server
echo     - this PC is set to NEVER SLEEP
echo.
echo   Results are written to results.csv after each heat.
echo   Review them with CHECK_RESULTS.bat
echo.
echo   To stop: close this window, then run STOP_OVERNIGHT.bat
echo            (that second step matters - it kills the trader and
echo             recorder, which are separate processes)
echo ============================================================
echo.

REM Refuse to start a second copy. Two traders on one account fight
REM each other and can build a position neither can hedge.
tasklist /FI "IMAGENAME eq python.exe" /FO CSV 2>NUL | find /I "python.exe" >NUL
if not errorlevel 1 (
    echo   WARNING: python is already running.
    echo   If the overnight runner is already going, close this window.
    echo   Otherwise run STOP_OVERNIGHT.bat first, then start again.
    echo.
    pause
)

python overnight.py

echo.
echo   Runner stopped. Run STOP_OVERNIGHT.bat to clear any leftovers.
pause

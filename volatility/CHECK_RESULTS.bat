@echo off
cd /d "%~dp0"
title RITCx Volatility - Results
python overnight.py --summary
echo.
echo   Recordings collected:
dir /b heats\*.jsonl 2>NUL | find /c ".jsonl"
echo.
pause

@echo off
REM ============================================================
REM  XAUUSD Trading Intelligence System — Task Scheduler Setup
REM  Run this as Administrator
REM ============================================================

echo.
echo ============================================================
echo   Setting up Windows Task Scheduler
echo ============================================================
echo.

setlocal enabledelayedexpansion

REM Get current directory
set "SCRIPT_DIR=%~dp0"
set "PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe"
set "V11_SCRIPT=%SCRIPT_DIR%execution\run_v11_daily.py"
set "V12_SCRIPT=%SCRIPT_DIR%execution\run_v12_hourly.py"

REM Check if running as admin
net session >nul 2>&1
if errorlevel 1 (
    echo ERROR: Please run this script as Administrator
    echo Right-click and select "Run as administrator"
    pause
    exit /b 1
)

echo [1/2] Creating V11 daily task (22:00 UTC)...
schtasks /create ^
    /tn "XAUUSD_V11_D1" ^
    /tr "\"%PYTHON%\" \"%V11_SCRIPT%\"" ^
    /sc daily ^
    /st 22:00 ^
    /f
if errorlevel 1 (
    echo WARNING: Failed to create V11 task
) else (
    echo   V11 daily task created
)

echo.
echo [2/2] Creating V12 hourly task (every hour)...
schtasks /create ^
    /tn "XAUUSD_V12_H1" ^
    /tr "\"%PYTHON%\" \"%V12_SCRIPT%\"" ^
    /sc hourly ^
    /st 00:05 ^
    /f
if errorlevel 1 (
    echo WARNING: Failed to create V12 task
) else (
    echo   V12 hourly task created
)

echo.
echo ============================================================
echo   Task Scheduler Setup Complete
echo ============================================================
echo.
echo   Tasks created:
echo   - XAUUSD_V11_D1:  Daily at 22:00 UTC
echo   - XAUUSD_V12_H1:  Every hour
echo.
echo   To verify: schtasks /query /tn "XAUUSD_V11_D1"
echo   To delete:  schtasks /delete /tn "XAUUSD_V11_D1" /f
echo.

pause
endlocal

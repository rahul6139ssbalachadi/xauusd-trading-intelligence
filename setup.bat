@echo off
REM ============================================================
REM  XAUUSD Trading Intelligence System — Client Setup Script
REM  Run this as Administrator
REM ============================================================

echo.
echo ============================================================
echo   XAUUSD Trading Intelligence System — Setup
echo ============================================================
echo.

setlocal enabledelayedexpansion

REM Check for Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed
    echo Install Python 3.11+ from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/7] Python found
python --version

REM Create virtual environment
echo.
echo [2/7] Creating virtual environment...
if not exist .venv (
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment
        pause
        exit /b 1
    )
    echo   Virtual environment created
) else (
    echo   Virtual environment already exists
)

REM Activate virtual environment
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo ERROR: Failed to activate virtual environment
    pause
    exit /b 1
)

REM Install dependencies
echo.
echo [3/7] Installing dependencies...
pip install --upgrade pip
pip install numpy pandas MetaTrader5 pytest
if errorlevel 1 (
    echo ERROR: Failed to install dependencies
    pause
    exit /b 1
)
echo   Dependencies installed

REM Run tests
echo.
echo [4/7] Running tests (this may take 2-3 minutes)...
.venv\Scripts\python.exe -m pytest tests/ -q > test_results.log 2>&1
if errorlevel 1 (
    echo.
    echo WARNING: Some tests failed. Check test_results.log for details.
    echo Continuing with setup...
) else (
    echo   All tests passed
    del test_results.log
)

REM Configure MT5
echo.
echo [5/7] Configuring MT5...
if not exist config\mt5.toml (
    echo.
    echo   Please provide your MT5 configuration:
    echo.
    set /p TERMINAL_PATH="   Path to terminal64.exe (e.g., C:\Program Files\XM Global MT5\terminal64.exe): "
    set /p DEMO_LOGIN="   Your demo account number: "
    set /p SYMBOL_NAME="   XAUUSD symbol name (e.g., GOLD.i#): "
    
    (
        echo # MT5 Configuration
        echo terminal_path = "!TERMINAL_PATH!"
        echo allowed_login = !DEMO_LOGIN!
        echo symbol = "!SYMBOL_NAME!"
    ) > config\mt5.toml
    
    echo   Configuration saved to config/mt5.toml
) else (
    echo   config/mt5.toml already exists
)

REM Test MT5 connection
echo.
echo [6/7] Testing MT5 connection...
.venv\Scripts\python.exe scripts/probe_mt5.py
if errorlevel 1 (
    echo.
    echo WARNING: MT5 connection failed. Please check config/mt5.toml
    echo You can continue setup and fix this later.
) else (
    echo   MT5 connection successful
)

REM Ingest historical data
echo.
echo [7/7] Ingesting historical data...
if exist scripts\ingest_extended_gold.py (
    .venv\Scripts\python.exe scripts\ingest_extended_gold.py
    if errorlevel 1 (
        echo   WARNING: Data ingestion had issues
    ) else (
        echo   Historical data ingested
    )
) else (
    echo   Script not found, skipping data ingestion
)

REM Enable live trading
echo.
echo ============================================================
echo   Setup Complete!
echo ============================================================
echo.
echo   Next steps:
echo   1. Review config/mt5.toml — make sure it's correct
echo   2. Edit config/settings.toml — set live_trading_enabled = true
echo   3. Run V11 daily: .venv\Scripts\python.exe execution\run_v11_daily.py
echo   4. Run V12 hourly: .venv\Scripts\python.exe execution\run_v12_hourly.py
echo.
echo   To set up automatic scheduling, run setup_tasks.bat as Administrator
echo.

pause
endlocal

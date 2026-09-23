# XAUUSD Trading Intelligence System — Client Setup

## Quick Start (Windows)

1. **Install Python 3.11+** from https://www.python.org/downloads/
2. **Install MetaTrader 5** from your broker
3. **Open Command Prompt as Administrator**
4. **Run the setup script:**

```
cd D:\
git clone https://github.com/rahul6139ssbalachadi/xauusd-trading-intelligence.git
cd xauusd-trading-intelligence
setup.bat
```

The setup script will:
- Create a Python virtual environment
- Install all dependencies (including MetaTrader5)
- Run the test suite (206 tests)
- Prompt you for your MT5 terminal path and demo account number
- Verify the MT5 connection
- Ingest historical data
- Set up automatic daily/hourly trading via Task Scheduler

---

## Manual Setup

If the automated script doesn't work, follow these steps:

### 1. Create virtual environment
```
python -m venv .venv
.venv\Scripts\activate
```

### 2. Install dependencies
```
pip install numpy pandas MetaTrader5 pytest
```

### 3. Configure MT5
Copy `config/mt5.toml.template` to `config/mt5.toml` and edit:
- `terminal_path`: Path to your MT5 terminal64.exe
- `allowed_login`: Your demo account number
- `symbol`: Your broker's XAUUSD symbol name

### 4. Test the connection
```
.venv\Scripts\python.exe scripts/probe_mt5.py
```

### 5. Ingest historical data
```
.venv\Scripts\python.exe scripts/ingest_extended_gold.py
```

### 6. Run tests
```
.venv\Scripts\python.exe -m pytest tests/ -q
```

### 7. Enable live trading
Edit `config/settings.toml`:
```
live_trading_enabled = true
```

### 8. Set up scheduled tasks

**V11 (D1 — daily at 22:00 UTC):**
```
schtasks /create /tn "V11_D1_Trading" /tr "D:\xauusd-trading-intelligence\.venv\Scripts\python.exe D:\xauusd-trading-intelligence\execution\run_v11_daily.py" /sc daily /st 22:00
```

**V12 (H1 — every hour):**
```
schtasks /create /tn "V12_H1_Trading" /tr "D:\xauusd-trading-intelligence\.venv\Scripts\python.exe D:\xauusd-trading-intelligence\execution\run_v12_hourly.py" /sc hourly /st 00:05
```

---

## Strategy Summary

| Strategy | Timeframe | Risk/Trade | Expected Signals | Status |
|----------|-----------|------------|------------------|--------|
| V11 | D1 (Daily) | 1% | 1-2/month | APPROVED |
| V12 | H1 (Hourly) | 2% | 14-16/month | APPROVED |

---

## Safety Features

- **Demo-only**: Refuses to run on real accounts
- **Kill switch**: Create `execution/KILL_SWITCH` file to stop all trading
- **No manual interference**: Won't trade if you have open positions on GOLD.i#
- **Max spread filter**: Won't trade if spread > 5 pips
- **Journal**: Every decision logged to `execution/journal.jsonl`

---

## Monitoring

Check the logs:
```
type execution\journal.jsonl
```

Check open positions in MT5.

To stop trading:
```
echo > execution\KILL_SWITCH
```

To restart:
```
del execution\KILL_SWITCH
```

---

## Support

For issues, check:
1. `execution/journal.jsonl` for blocked trades
2. MT5 terminal for error messages
3. Run `probe_mt5.py` to verify connection

---

## Important Disclaimings

- Past performance does not guarantee future results
- This is a research system, not a profit guarantee
- Always monitor live trading
- Start with a demo account
- Never risk more than you can afford to lose

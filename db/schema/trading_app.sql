-- Trading app tables: trades, equity_curve, performance_summary, risk_state
-- These support the API endpoints in api/trading.py

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('BUY', 'SELL')),
    entry_price REAL NOT NULL,
    exit_price REAL,
    stop_loss REAL,
    take_profit REAL,
    lots REAL NOT NULL DEFAULT 0.01,
    pnl_pips REAL,
    pnl_usd REAL,
    strategy TEXT,
    version TEXT,
    entry_time TIMESTAMP,
    exit_time TIMESTAMP,
    status TEXT DEFAULT 'open' CHECK (status IN ('open', 'closed', 'cancelled')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (client_id) REFERENCES clients(id)
);

CREATE TABLE IF NOT EXISTS equity_curve (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    equity REAL NOT NULL,
    FOREIGN KEY (client_id) REFERENCES clients(id)
);

CREATE TABLE IF NOT EXISTS performance_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL UNIQUE,
    total_trades INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0.0,
    profit_factor REAL DEFAULT 0.0,
    net_pnl_usd REAL DEFAULT 0.0,
    max_drawdown_pct REAL DEFAULT 0.0,
    current_strategy TEXT DEFAULT 'N/A',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (client_id) REFERENCES clients(id)
);

CREATE TABLE IF NOT EXISTS risk_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    daily_pnl_usd REAL DEFAULT 0.0,
    weekly_pnl_usd REAL DEFAULT 0.0,
    consecutive_losses INTEGER DEFAULT 0,
    kill_switch_active BOOLEAN DEFAULT 0,
    FOREIGN KEY (client_id) REFERENCES clients(id)
);

-- Insert sample trades for client 1
INSERT INTO trades (client_id, symbol, direction, entry_price, exit_price, stop_loss, take_profit, lots, pnl_pips, pnl_usd, strategy, version, entry_time, exit_time, status) VALUES
(1, 'XAUUSD', 'BUY', 2345.50, 2348.20, 2342.00, 2352.00, 0.1, 27.0, 27.0, 'V11', 'D1', '2026-06-01 10:00:00', '2026-06-03 14:00:00', 'closed'),
(1, 'XAUUSD', 'BUY', 2350.00, 2347.50, 2345.00, 2358.00, 0.1, -25.0, -25.0, 'V11', 'D1', '2026-06-05 09:00:00', '2026-06-05 18:00:00', 'closed'),
(1, 'XAUUSD', 'BUY', 2355.00, 2362.00, 2350.00, 2368.00, 0.1, 70.0, 70.0, 'V12', 'H1', '2026-06-10 12:00:00', '2026-06-11 08:00:00', 'closed'),
(1, 'XAUUSD', 'BUY', 2360.00, NULL, 2355.00, 2372.00, 0.1, NULL, NULL, 'V12', 'H1', '2026-06-15 10:00:00', NULL, 'open');

-- Insert sample trades for client 2
INSERT INTO trades (client_id, symbol, direction, entry_price, exit_price, stop_loss, take_profit, lots, pnl_pips, pnl_usd, strategy, version, entry_time, exit_time, status) VALUES
(2, 'XAUUSD', 'BUY', 2340.00, 2345.50, 2335.00, 2350.00, 0.2, 55.0, 110.0, 'V11', 'D1', '2026-06-02 11:00:00', '2026-06-04 15:00:00', 'closed'),
(2, 'XAUUSD', 'BUY', 2348.00, 2344.00, 2342.00, 2356.00, 0.2, -40.0, -80.0, 'V12', 'H1', '2026-06-08 14:00:00', '2026-06-08 20:00:00', 'closed');

-- Insert equity curve data
INSERT INTO equity_curve (client_id, date, equity) VALUES
(1, '2026-06-01', 1000.00),
(1, '2026-06-02', 1010.00),
(1, '2026-06-03', 1027.00),
(1, '2026-06-04', 1015.00),
(1, '2026-06-05', 1002.00),
(1, '2026-06-06', 1005.00),
(1, '2026-06-07', 1010.00),
(1, '2026-06-08', 1020.00),
(1, '2026-06-09', 1035.00),
(1, '2026-06-10', 1050.00);

INSERT INTO equity_curve (client_id, date, equity) VALUES
(2, '2026-06-01', 2000.00),
(2, '2026-06-02', 2050.00),
(2, '2026-06-03', 2110.00),
(2, '2026-06-04', 2090.00),
(2, '2026-06-05', 2080.00),
(2, '2026-06-06', 2085.00),
(2, '2026-06-07', 2095.00),
(2, '2026-06-08', 2075.00),
(2, '2026-06-09', 2080.00),
(2, '2026-06-10', 2090.00);

-- Insert performance summaries
INSERT INTO performance_summary (client_id, total_trades, win_rate, profit_factor, net_pnl_usd, max_drawdown_pct, current_strategy) VALUES
(1, 4, 0.75, 2.1, 72.0, 2.5, 'V11 D1 + V12 H1');

INSERT INTO performance_summary (client_id, total_trades, win_rate, profit_factor, net_pnl_usd, max_drawdown_pct, current_strategy) VALUES
(2, 2, 0.5, 1.375, 30.0, 1.5, 'V11 D1');

-- Insert risk state
INSERT INTO risk_state (client_id, daily_pnl_usd, weekly_pnl_usd, consecutive_losses, kill_switch_active) VALUES
(1, 15.00, 72.00, 0, 0);

INSERT INTO risk_state (client_id, daily_pnl_usd, weekly_pnl_usd, consecutive_losses, kill_switch_active) VALUES
(2, -10.00, 30.00, 1, 0);

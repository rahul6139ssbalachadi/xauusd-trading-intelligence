-- Dashboard schema migration: V1 — read-side tables only.
--
-- This migration adds 5 NEW tables for the trading dashboard companion UI.
-- It does NOT modify any existing table (market_data is untouched).
-- The control_commands table is the ONLY write path — it is a one-way
-- dashboard-to-engine signal table, not a direct state mutation.
--
-- Apply: ./.venv/Scripts/python.exe -c "
--   import sqlite3; c=sqlite3.connect('db/trading.db');
--   c.executescript(open('db/schema/dashboard_v1.sql').read());
--   c.commit(); c.close(); print('migration applied')
-- "

-- =============================================================================
-- signals_log — every signal generated, taken or filtered, with filter reason
-- =============================================================================
CREATE TABLE IF NOT EXISTS signals_log (
    id              INTEGER     PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT        NOT NULL,          -- e.g. "XAUUSD"
    timeframe       TEXT        NOT NULL,          -- e.g. "D1"
    strategy_name   TEXT        NOT NULL,          -- e.g. "V11_D1_MOMENTUM"
    strategy_version TEXT       NOT NULL,          -- e.g. "V11"
    timestamp       TEXT        NOT NULL,          -- ISO 8601 UTC when signal generated
    signal          TEXT        NOT NULL,          -- "BUY" | "SELL" | "WAIT"
    confidence      REAL,                          -- 0-100
    entry_price     REAL,                          -- planned entry (NULL if WAIT)
    stop_price      REAL,                          -- planned stop (NULL if WAIT)
    target_price    REAL,                          -- planned target (NULL if WAIT)
    lots            REAL,                          -- planned position size (NULL if WAIT)
    risk_usd        REAL,                          -- planned risk in USD (NULL if WAIT)
    reason          TEXT,                          -- if WAIT: why the signal was filtered
    regime          TEXT,                          -- market regime at signal time
    spread_pips     REAL,                          -- spread in pips at signal time
    filter_flags    TEXT,                          -- JSON array of filter reasons applied
    created_at      TEXT        DEFAULT (datetime('now', 'utc'))
);

CREATE INDEX IF NOT EXISTS idx_signals_ts      ON signals_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_signals_strat   ON signals_log(strategy_name, strategy_version);
CREATE INDEX IF NOT EXISTS idx_signals_symbol  ON signals_log(symbol, timeframe);

-- =============================================================================
-- equity_curve — periodic balance/equity/drawdown snapshots
-- =============================================================================
CREATE TABLE IF NOT EXISTS equity_curve (
    id              INTEGER     PRIMARY KEY AUTOINCREMENT,
    ts              TEXT        NOT NULL,          -- ISO 8601 UTC timestamp of snapshot
    strategy_name   TEXT        NOT NULL,          -- strategy that produced this equity
    strategy_version TEXT       NOT NULL,
    balance         REAL        NOT NULL,          -- account balance (no unrealized pos)
    equity          REAL        NOT NULL,          -- balance + unrealized P&L
    drawdown_pct    REAL,                          -- % drawdown from peak equity
    drawdown_usd    REAL,                          -- $ drawdown from peak equity
    net_pnl         REAL,                          -- cumulative net P&L since start
    created_at      TEXT        DEFAULT (datetime('now', 'utc'))
);

CREATE INDEX IF NOT EXISTS idx_equity_ts     ON equity_curve(ts DESC);
CREATE INDEX IF NOT EXISTS idx_equity_strat  ON equity_curve(strategy_name, strategy_version);

-- =============================================================================
-- risk_state — current risk snapshot (single row, updated live)
-- =============================================================================
CREATE TABLE IF NOT EXISTS risk_state (
    id              INTEGER     PRIMARY KEY AUTOINCREMENT,
    as_of_ts        TEXT        NOT NULL,          -- when this snapshot was taken
    account_equity  REAL        NOT NULL,          -- current equity
    account_balance REAL        NOT NULL,          -- current balance
    daily_pnl       REAL        NOT NULL,          -- P&L since start of day
    daily_loss_cap  REAL        NOT NULL,          -- configured daily loss limit (USD)
    daily_loss_remaining REAL   NOT NULL,          -- remaining daily loss budget
    consecutive_losses INTEGER  NOT NULL,          -- current consecutive loss count
    max_consecutive_allowed INTEGER NOT NULL,      -- configured max consecutive losses
    open_positions  INTEGER    NOT NULL,          -- current open position count
    max_positions   INTEGER    NOT NULL,          -- configured max simultaneous positions
    correlated_exposure REAL    NOT NULL,          -- current portfolio-level exposure
    max_correlated_exposure REAL NOT NULL,         -- configured max correlated exposure
    risk_pct_per_trade REAL    NOT NULL,          -- configured risk per trade (%)
    is_kill_switch  INTEGER    NOT NULL DEFAULT 0, -- 0=off, 1=kill switch triggered
    is_trading_paused INTEGER  NOT NULL DEFAULT 0,  -- 0=active, 1=pause by user/engine
    last_kill_reason TEXT,                          -- reason for kill switch if active
    updated_at      TEXT        DEFAULT (datetime('now', 'utc'))
);

CREATE INDEX IF NOT EXISTS idx_risk_ts ON risk_state(as_of_ts DESC);

-- =============================================================================
-- control_commands — dashboard-to-engine one-way command log
-- =============================================================================
-- This is the ONLY dashboard-initiated write path. Commands are written here
-- by the dashboard backend; the trading engine polls this table and acts on
-- the latest pending command. The engine never writes back — no feedback
-- loop from engine to dashboard through this table.
CREATE TABLE IF NOT EXISTS control_commands (
    id              INTEGER     PRIMARY KEY AUTOINCREMENT,
    command         TEXT        NOT NULL,          -- "KILL_SWITCH_ON" | "KILL_SWITCH_OFF" | "PAUSE_STRATEGY" | "RESUME_STRATEGY" | "STOP_TRADING"
    strategy_name   TEXT,                          -- target strategy (NULL = global)
    strategy_version TEXT,                         -- target version (NULL = all)
    reason          TEXT        NOT NULL,          -- human-readable reason for the command
    requested_by    TEXT        NOT NULL,          -- dashboard user / session identifier
    status          TEXT        NOT NULL DEFAULT 'pending', -- "pending" | "acknowledged" | "executed"
    executed_by     TEXT,                          -- engine identifier that executed it
    executed_at     TEXT,                          -- when the engine acknowledged/executed it
    created_at      TEXT        DEFAULT (datetime('now', 'utc'))
);

CREATE INDEX IF NOT EXISTS idx_ctrl_created ON control_commands(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ctrl_pending ON control_commands(status) WHERE status = 'pending';

-- =============================================================================
-- strategy_registry — strategy metadata + performance summary
-- =============================================================================
CREATE TABLE IF NOT EXISTS strategy_registry (
    id              INTEGER     PRIMARY KEY AUTOINCREMENT,
    name            TEXT        NOT NULL,          -- e.g. "V11_D1_MOMENTUM"
    version         TEXT        NOT NULL,          -- e.g. "V11"
    file_path       TEXT        NOT NULL,          -- relative to project root
    status          TEXT        NOT NULL DEFAULT 'research', -- research|backtesting|validation|paper_trading|approved|rejected|archived
    created_at      TEXT        DEFAULT (datetime('now', 'utc')),
    last_modified   TEXT        DEFAULT (datetime('now', 'utc')),
    last_modified_by TEXT,
    -- performance summary (populated from validation/backtest results)
    train_net_pips  REAL,                          -- training set net P&L (pips)
    train_pf        REAL,                          -- training profit factor
    train_win_rate  REAL,                          -- training win rate
    train_trades    INTEGER,                       -- training trade count
    val_net_pips    REAL,                          -- validation net P&L
    val_pf          REAL,                          -- validation profit factor
    val_win_rate    REAL,                          -- validation win rate
    val_trades      INTEGER,                       -- validation trade count
    oos_net_pips    REAL,                          -- out-of-sample (walk-forward) net P&L
    oos_pf          REAL,                          -- out-of-sample profit factor
    oos_win_rate    REAL,                          -- out-of-sample win rate
    oos_trades      INTEGER,                       -- out-of-sample trade count
    wf_degradation  REAL,                          -- walk-forward degradation (IS vs OOS)
    mc_net_p5       REAL,                          -- Monte Carlo 5th percentile net
    mc_pf_p5        REAL,                          -- Monte Carlo 5th percentile PF
    mc_is_robust    INTEGER,                       -- 0 or 1 (boolean)
    mc_ruin_prob    REAL,                          -- Monte Carlo ruin probability
    full_net_pips   REAL,                          -- full backtest net P&L
    full_pf         REAL,                          -- full profit factor
    full_win_rate   REAL,                          -- full win rate
    full_trades     INTEGER,                       -- full trade count
    full_sharpe     REAL,                          -- full Sharpe ratio
    full_max_dd     REAL,                          -- full max drawdown (pips)
    rejection_reason TEXT,                         -- if rejected: the reason
    notes           TEXT,                          -- free-form notes
    -- uniqueness constraint: one row per strategy+version
    UNIQUE(name, version)
);

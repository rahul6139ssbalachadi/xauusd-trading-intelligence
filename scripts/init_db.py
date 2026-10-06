#!/usr/bin/env python3
"""
Database initialization script.
Creates the required schema for the trading intelligence system.
"""

import sqlite3
import os
from pathlib import Path

DATABASE_PATH = os.environ.get('DATABASE_PATH', 'db/trading.db')

def create_schema():
    """Create database schema if it doesn't exist."""
    
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    # Create tables
    cursor.executescript('''
    CREATE TABLE IF NOT EXISTS market_data (
        timestamp INTEGER,
        symbol TEXT,
        timeframe TEXT,
        open REAL,
        high REAL,
        low REAL,
        close REAL,
        volume INTEGER,
        spread REAL,
        PRIMARY KEY (timestamp, symbol, timeframe)
    );
    
    CREATE TABLE IF NOT EXISTS signals_log (
        timestamp INTEGER PRIMARY KEY,
        symbol TEXT,
        decision TEXT,
        entry_price REAL,
        stop_loss REAL,
        take_profit REAL,
        confidence REAL,
        strategy TEXT,
        risk_pct REAL,
        equity REAL,
        evidence TEXT
    );
    
    CREATE TABLE IF NOT EXISTS equity_curve (
        timestamp INTEGER PRIMARY KEY,
        equity REAL,
        balance REAL
    );
    
    CREATE TABLE IF NOT EXISTS risk_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        daily_loss REAL DEFAULT 0,
        weekly_loss REAL DEFAULT 0,
        consecutive_losses INTEGER DEFAULT 0,
        current_positions INTEGER DEFAULT 0,
        total_risk_pct REAL DEFAULT 0
    );
    
    CREATE TABLE IF NOT EXISTS control_commands (
        timestamp INTEGER,
        command TEXT,
        value TEXT
    );
    
    CREATE TABLE IF NOT EXISTS strategy_registry (
        name TEXT PRIMARY KEY,
        version TEXT,
        market TEXT,
        created_at INTEGER,
        status TEXT,
        params TEXT
    );
    
    CREATE TABLE IF NOT EXISTS clients (
        id INTEGER PRIMARY KEY,
        name TEXT,
        api_key TEXT,
        created_at INTEGER
    );
    
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        username TEXT UNIQUE,
        email TEXT UNIQUE,
        hashed_password TEXT,
        disabled INTEGER DEFAULT 0
    );
    
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY,
        strategy TEXT,
        symbol TEXT,
        timestamp REAL,
        direction TEXT,
        entry_price REAL,
        exit_price REAL,
        size REAL,
        profit_loss REAL,
        status TEXT
    );
    
    CREATE TABLE IF NOT EXISTS performance_summary (
        date TEXT PRIMARY KEY,
        strategy TEXT,
        trades INTEGER,
        win_rate REAL,
        profit_factor REAL,
        net_return REAL,
        sharpe REAL,
        max_drawdown REAL
    );
    ''')
    
    conn.commit()
    conn.close()
    print(f"Database initialized at {DATABASE_PATH}")

if __name__ == '__main__':
    create_schema()
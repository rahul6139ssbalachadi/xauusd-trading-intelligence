"""Database indexes and constraints for performance."""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "db" / "trading.db"


def add_indexes():
    """Add performance indexes to existing tables."""
    conn = sqlite3.connect(DB_PATH)
    
    # Indexes for common queries
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_trades_client_id ON trades(client_id)",
        "CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)",
        "CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time)",
        "CREATE INDEX IF NOT EXISTS idx_equity_curve_client_date ON equity_curve(client_id, date)",
        "CREATE INDEX IF NOT EXISTS idx_risk_state_client ON risk_state(client_id)",
        "CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)",
        "CREATE INDEX IF NOT EXISTS idx_users_client_id ON users(client_id)",
    ]
    
    for idx_sql in indexes:
        try:
            conn.execute(idx_sql)
        except Exception as e:
            print(f"Index creation note: {e}")
    
    conn.commit()
    conn.close()
    print("Indexes verified/created")


if __name__ == "__main__":
    add_indexes()

"""Quick read-only summary of db/trading.db contents."""
import sqlite3
from datetime import datetime, timezone

con = sqlite3.connect("db/trading.db")
cur = con.cursor()
cur.execute("SELECT symbol, timeframe, COUNT(*) as n FROM market_data GROUP BY symbol, timeframe ORDER BY timeframe")
print("Row counts:")
for row in cur.fetchall():
    print(f"  {row}")

print("\nDate ranges:")
for tf in ["M1", "M5", "M15", "H1", "D1"]:
    cur.execute("SELECT MIN(ts_broker_epoch), MAX(ts_broker_epoch) FROM market_data WHERE timeframe=?", (tf,))
    r = cur.fetchone()
    if r[0]:
        print(f"  {tf:4s}: {datetime.fromtimestamp(r[0], tz=timezone.utc).date()} -> {datetime.fromtimestamp(r[1], tz=timezone.utc).date()}  ({r[1]-r[0]} seconds span)")
con.close()
print("\nDONE")

import sqlite3
import datetime as dt

c = sqlite3.connect("db/trading.db")
f = lambda e: dt.datetime.fromtimestamp(int(e), tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
ORDER = "CASE timeframe WHEN 'M1' THEN 1 WHEN 'M5' THEN 2 WHEN 'M15' THEN 3 WHEN 'M30' THEN 4 WHEN 'H1' THEN 5 WHEN 'H4' THEN 6 WHEN 'D1' THEN 7 END"

for sym in ("XAUUSD", "BTCUSD"):
    print(f"=== {sym} FINAL STATE ===")
    q = ("SELECT timeframe, COUNT(*), MIN(ts_broker_epoch), MAX(ts_broker_epoch), "
         "COUNT(DISTINCT ts_broker_epoch) FROM market_data "
         "WHERE symbol=? AND source='mt5' GROUP BY timeframe ORDER BY " + ORDER)
    for tf, n, mn, mx, distinct in c.execute(q, (sym,)):
        print(f"  {tf:>4}  {n:>7d} bars  {f(mn)}  ->  {f(mx)}   dups={n - distinct}")
    print()

print("=== XAUUSD LAST 3 D1 BARS ===")
for r in c.execute("SELECT ts_broker_epoch, open, high, low, close, tick_volume, spread "
                   "FROM market_data WHERE symbol='XAUUSD' AND timeframe='D1' "
                   "ORDER BY ts_broker_epoch DESC LIMIT 3"):
    print(f"  {f(r[0])}  O={r[1]:.2f} H={r[2]:.2f} L={r[3]:.2f} C={r[4]:.2f} "
          f"vol={r[5]} spread={r[6]}")
print()

print("=== XAUUSD LAST 2 H4 BARS ===")
for r in c.execute("SELECT ts_broker_epoch, open, high, low, close, tick_volume, spread "
                   "FROM market_data WHERE symbol='XAUUSD' AND timeframe='H4' "
                   "ORDER BY ts_broker_epoch DESC LIMIT 2"):
    print(f"  {f(r[0])}  O={r[1]:.2f} H={r[2]:.2f} L={r[3]:.2f} C={r[4]:.2f} "
          f"vol={r[5]} spread={r[6]}")
print()

print("=== XAUUSD LAST 2 M1 BARS ===")
for r in c.execute("SELECT ts_broker_epoch, open, high, low, close, tick_volume, spread "
                   "FROM market_data WHERE symbol='XAUUSD' AND timeframe='M1' "
                   "ORDER BY ts_broker_epoch DESC LIMIT 2"):
    print(f"  {f(r[0])}  O={r[1]:.2f} H={r[2]:.2f} L={r[3]:.2f} C={r[4]:.2f} "
          f"vol={r[5]} spread={r[6]}")

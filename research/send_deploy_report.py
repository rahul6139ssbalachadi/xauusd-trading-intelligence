"""Send the session report to Telegram. Exits non-zero if the send fails.

A silently undelivered report is worse than a noisy failure, so the exit
code is the contract: 0 = delivered, 1 = not delivered.

Credentials are read from the Hermes bridge (the single source of truth) at
call time. Never printed, never duplicated into this file.
"""
from __future__ import annotations

import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BRIDGE = Path(r"D:\rahul_ai\hermes\hermes_telegram_bridge.py")


def creds() -> tuple[str, str]:
    src = BRIDGE.read_text(encoding="utf-8", errors="ignore")
    tok = re.search(r'BOT_TOKEN\s*=\s*"([^"]+)"', src)
    cid = re.search(r'CHAT_ID\s*=\s*"([^"]+)"', src)
    if not tok or not cid:
        raise SystemExit("could not read Telegram credentials")
    return tok.group(1), cid.group(1)


REPORT = """<b>DEPLOYMENT READY - V11/V12 DEMO runner</b>

<b>Committed &amp; pushed</b>
a798dc4 -&gt; origin/master
76 files, +13,960 lines

<b>SAFETY CHANGE (the important one)</b>
One flag meant both "run orders" and "live allowed".
Now split into two with opposite polarity:
  demo_execution_enabled = true   (permits orders on verified DEMO only)
  live_trading_enabled = false    (NOT a permission - true BLOCKS)

mt5_gateway.py unchanged. Risk limits, V11/V12 logic and research results all untouched.

<b>Safety matrix (test-verified)</b>
  Correct DEMO 345982869 -&gt; EXECUTED
  Wrong login            -&gt; BLOCKED
  REAL account           -&gt; BLOCKED
  live=true              -&gt; BLOCKED
  demo=false             -&gt; BLOCKED

<b>Tests</b> 489 passed, 0 regressions (was 483)

<b>V11/V12 May-Aug backtest (XAUUSD)</b>
V11: 1 signal in 4 months, lost it. 82 signals over 10y = ~8/yr, so this is expected.
V12: 17 signals, 14 trades, 57.1% win, +$13,915

<b>Best candidate</b>
V11 hold=2d: OOS PF 3.82 (vs 1.82 deployed), degradation -1.15, MC robust.
Clear every gate, but n=17 - paper trade before trusting.

<b>Two findings worth your attention</b>
1. V11's min_atr_pct=0.005 is BELOW the D1 ATR/close floor (0.0073).
   The filter can never fire. It is a no-op right now.

2. V12 FAILS the repo's own §25 gate: in-sample PF 0.98 (needs 1.20).
   Not a split artifact - confirmed across every split. Its edge exists
   only in the last ~20 months. It is live-approved but under-evidenced.
   Your call - not recommending you change it.

<b>Lot size</b>
Cannot change profit factor at all (scales P&amp;L linearly).
Percentage risk beats fixed lots: PF 2.20 vs 1.82 on V11.
Keep the engine's percentage sizing.

<b>NOT verified</b>
The live MT5 path. Terminal was logged out (-6) during testing.
Dry-run on the new PC before trusting it with real demo orders."""


def main() -> int:
    token, chat_id = creds()
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": REPORT, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        with urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=data, timeout=30) as r:
            body = r.read().decode()
    except Exception as exc:  # noqa: BLE001
        print(f"TELEGRAM FAILED: {type(exc).__name__}: {exc}")
        return 1
    print("Telegram: sent" if '"ok":true' in body.replace(" ", "") else f"Telegram: FAILED {body[:200]}")
    return 0 if '"ok":true' in body.replace(" ", "") else 1


if __name__ == "__main__":
    sys.exit(main())

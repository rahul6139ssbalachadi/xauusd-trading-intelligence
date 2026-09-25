"""Daily trading + activity update, self-sent to Telegram.

Replaces the agent-driven 'Daily Trading Update' cron job, which failed with
"no delivery target resolved for deliver=telegram" (deliver='telegram' cannot
resolve a target on this machine unless the Hermes gateway/bridge is running).

Runs the read-only account status (balance, equity, open positions, gold spot,
V11/V12 signal state) and appends recent journal activity. no_agent=True, so
there is no LLM call and no token cost. Stdout is the message body; the
script sends it itself, so gateway state is irrelevant.

  scheduled_daily.py            # full update
  scheduled_daily.py --no-tg    # print only (debug)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

JOURNAL = ROOT / "execution" / "journal.jsonl"


def recent_activity(limit: int = 5) -> list[str]:
    """Summarise the last few journal decisions. Read-only file tail."""
    if not JOURNAL.exists():
        return ["journal: not created yet"]
    lines = [ln for ln in JOURNAL.read_text(encoding="utf-8",
                                            errors="ignore").splitlines() if ln.strip()]
    out = []
    for raw in lines[-limit:]:
        try:
            e = json.loads(raw)
        except json.JSONDecodeError:
            out.append("  (unparseable journal line)")
            continue
        action = e.get("action") or e.get("decision") or "?"
        out.append(f"  {str(e.get('ts', ''))[:16]}  {action}  {e.get('reason', '')[:70]}")
    if not out:
        out = ["  (journal empty)"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-tg", action="store_true", help="print only, do not send")
    args = ap.parse_args()

    try:
        from execution.account_status import collect
        status, _ = collect()
    except Exception as exc:  # noqa: BLE001
        status = f"account status unavailable: {type(exc).__name__}: {exc}"

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    text = "\n".join([
        "<b>DAILY TRADING UPDATE</b>",
        f"<i>{stamp}</i>",
        "",
        status,
        "",
        "<b>RECENT DECISIONS (journal)</b>",
        *recent_activity(),
    ])

    if args.no_tg:
        print(text)
        return 0

    from execution.account_status import deliver
    return deliver(text, "daily update")


if __name__ == "__main__":
    sys.exit(main())

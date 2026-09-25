"""Scheduled live runner + Telegram update for one strategy.

Run from cron (no_agent=True). Executes the V11 or V12 live runner against the
MT5 terminal, appends the decision to the engine journal, and self-sends an
account+trade update to Telegram — delivery does NOT depend on the Hermes
gateway (deliver='telegram' is unreliable here).

Stdout is the notification body (cron treats empty stdout as silence), with a
`Telegram: sent|FAILED` suffix so a failed send is visible in the cron log.

  scheduled_live.py V11   # D1, once per day after the bar closes
  scheduled_live.py V12   # H1, every hour after the bar closes
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
RUNNERS = {"V11": "execution/run_v11_daily.py", "V12": "execution/run_v12_hourly.py"}
INTEREST = ("decision", "signal", "EXECUTED", "BLOCKED", "plan", "account",
            "bars", "checklist", "V1", "V12")


def _run(script: str) -> str:
    p = subprocess.run([PY, str(ROOT / script)], capture_output=True,
                       text=True, timeout=300, cwd=str(ROOT))
    return (p.stdout or "").strip() or (p.stderr or "").strip()


def main() -> int:
    version = sys.argv[1].upper()
    if version not in RUNNERS:
        print(f"usage: {Path(__file__).name} {'|'.join(RUNNERS)}")
        return 2

    out = _run(RUNNERS[version])
    headlines = [ln for ln in out.splitlines() if any(k in ln for k in INTEREST)]
    body = "\n".join([f"<b>{version} LIVE CHECK</b>"] + headlines)

    try:
        from execution.account_status import collect
        status, _ = collect()
    except Exception as exc:  # noqa: BLE001
        status = f"account status unavailable: {type(exc).__name__}: {exc}"

    text = f"{body}\n\n{status}"
    from execution.account_status import deliver
    print(body)
    return deliver(text, version)


if __name__ == "__main__":
    sys.exit(main())

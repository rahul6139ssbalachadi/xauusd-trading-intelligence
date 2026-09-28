"""Structured logging setup (Phase 12).

Design rules, in priority order:

  1. NEVER log a secret. The filter below is a hard backstop that runs on
     every record regardless of how it was produced, because a logging
     mistake should not become a credential leak. It is not a substitute
     for care at the call site.

  2. Levels must be meaningful, not decorative. INFO is normal operation,
     WARNING is a condition a human should know about, ERROR is a failed
     operation that was handled, CRITICAL is the system cannot continue
     (or, here, a safety control was violated).

  3. Logs must be usable from a server. Under Docker the default is
     stdout+JSON so `docker logs` and any log shipper can parse them.
     A file sink is available for non-container deployments.

Usage:
    from observability import setup_logging
    setup_logging()            # reads LOG_LEVEL / LOG_FORMAT / LOG_FILE
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from appconfig import get_config

# Keys whose VALUES must never appear in a log, matched case-insensitively
# against the extra/context dicts we attach to records.
_SECRET_KEY_RE = re.compile(
    r"(pass(word)?|secret|token|api[_-]?key|authorization|auth|credential|"
    r"mt5[_-]?(login|password)|private[_-]?key|bearer)",
    re.IGNORECASE,
)
_MASK = "***REDACTED***"

# The literal token header name used by the bridge adapter. Redacted even
# though it does not match the key regex above, as a named belt.
_SENSITIVE_KEYS = {"x-bridge-token", "bridge_token", "x_bridge_token"}


class SecretFilter(logging.Filter):
    """Masks secret-looking values in a record and its message.

    Applied to the ROOT logger, so every library in the process is covered
    as well as our own loggers. It rewrites the formatted message rather
    than mutating caller arguments, which keeps behaviour predictable.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # 1. scrub structured extras
        for attr in ("extra_data", "context", "data"):
            payload = getattr(record, attr, None)
            if isinstance(payload, dict):
                setattr(record, attr, _scrub_dict(payload))
        # 2. scrub known-sensitive record attributes
        for key in _SENSITIVE_KEYS:
            if hasattr(record, key):
                setattr(record, key, _MASK)
        # 3. scrub the already-formatted message
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging raise
            return True
        if msg and any(tok in msg for tok in ("password", "token", "secret", "Authorization")):
            msg = _scrub_inline(msg)
            record.msg = msg
            record.args = ()
        return True


def _scrub_dict(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        key = str(k)
        if key.lower() in _SENSITIVE_KEYS or _SECRET_KEY_RE.search(key):
            out[k] = _MASK
        elif isinstance(v, dict):
            out[k] = _scrub_dict(v)
        else:
            out[k] = v
    return out


def _scrub_inline(msg: str) -> str:
    """Best-effort inline scrub of `key=value` / `key: value` patterns."""
    for key in ("password", "token", "secret", "api_key", "apikey", "authorization"):
        msg = re.sub(
            rf"({key}\s*[=:]\s*)(\S+)",
            rf"\1{_MASK}",
            msg,
            flags=re.IGNORECASE,
        )
    return msg


class JsonFormatter(logging.Formatter):
    """One JSON object per line. `extra={...}` fields are merged in.

    Timestamp is UTC ISO-8601 with a 'Z' so it sorts lexicographically and
    needs no interpretation.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
                  .isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for attr in ("extra_data", "context", "data"):
            extra = getattr(record, attr, None)
            if isinstance(extra, dict):
                payload.update(_scrub_dict(extra))
        return json.dumps(payload, default=str)


class RedactingFormatter(logging.Formatter):
    """Human-readable single-line formatter for local/dev use."""

    def __init__(self, fmt: str | None = None):
        # Without this, logging.Formatter.formatTime() receives a strftime
        # string but the base class is still in '%' style, and validation
        # rejects the format. Passing it as the positional `fmt` fixes it.
        super().__init__(fmt or "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                         datefmt="%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = (f"{self.formatTime(record, self.datefmt)} {record.levelname:<8} "
                f"{record.name}: {record.getMessage()}")
        for attr in ("extra_data", "context", "data"):
            extra = getattr(record, attr, None)
            if isinstance(extra, dict) and extra:
                base += " " + " ".join(f"{k}={v}" for k, v in _scrub_dict(extra).items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def setup_logging(level: str | None = None, fmt: str | None = None,
                  log_file: str | None = None) -> logging.Logger:
    """Configure the root logger. Idempotent — safe to call twice."""
    cfg = get_config()
    level = (level or cfg.log_level or "INFO").upper()
    fmt = (fmt or cfg.log_format or "text").lower()
    log_file = log_file if log_file is not None else cfg.log_file

    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    for f in root.filters:
        root.removeFilter(f)

    secret_filter = SecretFilter()
    root.addFilter(secret_filter)

    if fmt == "json":
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = RedactingFormatter()

    stream = logging.StreamHandler(stream=sys.stdout)
    stream.setFormatter(formatter)
    stream.addFilter(secret_filter)
    root.addHandler(stream)

    if log_file:
        path = Path(log_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
            )
            fh.setFormatter(formatter)
            fh.addFilter(secret_filter)
            root.addHandler(fh)
        except OSError as exc:
            # Logging must never be the reason the process dies.
            root.warning("could not open log file %s: %s", path, exc)

    # Third-party loggers are noisy at INFO and can echo URLs/tokens.
    for noisy in ("urllib3", "httpx", "httpcore", "asyncio", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


__all__ = [
    "JsonFormatter", "RedactingFormatter", "SecretFilter", "get_logger",
    "setup_logging",
]

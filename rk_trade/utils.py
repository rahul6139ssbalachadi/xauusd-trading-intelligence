"""Atomic file write + file locking utilities for RK Trade.

Provides:
  - atomic_write_json / atomic_write_text: write-then-rename for crash safety
  - file_lock: context manager using fcntl (Unix) or msvcrt (Windows)
  - safe_read_json: lock + read JSON with error recovery

These prevent corrupt JSON when multiple CLI processes write to the
same user directory concurrently (e.g. two users running `live` simultaneously
on the same machine — rare but possible).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from contextlib import contextmanager

# Platform-specific locking
try:
    import fcntl  # Unix
    _HAS_FCTL = True
except ImportError:
    import msvcrt  # Windows
    _HAS_FCTL = False
    # Windows constants for msvcrt.locking
    _LK_LOCK = 1   # lock, blocking
    _LK_UNL = 2    # unlock


@contextmanager
def file_lock(lock_path: str):
    """Cross-platform file lock context manager.

    Creates a lock file and holds an exclusive lock on it. The lock
    is automatically released when the context exits. Safe for use
    across processes.
    """
    lock_file = open(lock_path, "w")
    try:
        if _HAS_FCTL:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        else:
            msvcrt.locking(lock_file.fileno(), _LK_LOCK, 1)
        yield
    finally:
        if _HAS_FCTL:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        else:
            try:
                msvcrt.locking(lock_file.fileno(), _LK_UNL, 1)
            except OSError:
                pass
        lock_file.close()


def atomic_write_text(path: str | Path, content: str, encoding: str = "utf-8") -> None:
    """Write text atomically: write to temp file, then rename.

    This ensures the file is never observed in a partially-written state.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # write to temp file in the same directory (so rename is atomic on same FS)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=path.name + "."
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        os.replace(tmp_path, str(path))
    except Exception:
        # clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_json(path: str | Path, data, indent: int = 2) -> None:
    """Write JSON atomically."""
    atomic_write_text(path, json.dumps(data, indent=indent))


def safe_read_json(path: str | Path, default=None):
    """Read JSON with locking. Returns `default` if file doesn't exist."""
    path = Path(path)
    if not path.exists():
        return default
    lock_path = str(path) + ".lock"
    try:
        with file_lock(lock_path):
            return json.loads(path.read_text())
    except (json.JSONDecodeError, IOError):
        return default


def atomic_append_jsonl(path: str | Path, row: dict) -> None:
    """Append a JSONL row atomically (single line, append mode is already atomic
    on most POSIX systems for small writes, but we use a lock for cross-platform safety)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row) + "\n"
    lock_path = str(path) + ".lock"
    with file_lock(lock_path):
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

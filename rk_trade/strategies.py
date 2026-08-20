"""Per-user strategy registry for RK Trade.

Users can register strategy definitions (existing JSON files from the
strategy engine) under their own namespace. The registry maps a user's
strategy alias to the source JSON file, so users can A/B test different
strategies on the live feed without touching the global strategy defs.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from rk_trade import STRATEGIES_DIR
from rk_trade.users import UserManager
from rk_trade.utils import atomic_write_json, safe_read_json, file_lock


class StrategyRegistry:
    """Register and retrieve user strategies from local JSON files."""

    def __init__(self, user_mgr: UserManager):
        self.um = user_mgr

    def _registry_path(self, username: str) -> Path:
        return self.um.strategy_dir(username) / "registry.json"

    def _lock_path(self, username: str) -> str:
        return str(self._registry_path(username)) + ".lock"

    def _load_registry_unlocked(self, username: str) -> dict:
        """Load registry without locking (caller must hold lock)."""
        path = self._registry_path(username)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, IOError):
            return {}

    def _save_registry_unlocked(self, username: str, reg: dict) -> None:
        """Save registry atomically without locking (caller must hold lock)."""
        atomic_write_json(self._registry_path(username), reg)

    def _load_registry(self, username: str) -> dict:
        """Load registry with file locking + error recovery (standalone use)."""
        return safe_read_json(self._registry_path(username), default={})

    def register(self, username: str, name: str, source_file: str | Path,
                 is_active: bool = True) -> Path:
        """Copy a strategy JSON into the user's strategy dir and register it.

        `name` is the user's alias for this strategy (e.g. "my_breakout").
        Validates the source is a valid Strategy JSON before copying.

        Returns the path to the copied strategy file.
        """
        source = Path(source_file)
        if not source.exists():
            raise FileNotFoundError(f"Source strategy file not found: {source}")

        # Validate strategy JSON (read-only, no write lock)
        strat_def = safe_read_json(source)
        if strat_def is None:
            raise ValueError(f"Source file is not valid JSON: {source}")
        if not isinstance(strat_def, dict) or "name" not in strat_def or "version" not in strat_def:
            raise ValueError(f"Source is not a valid Strategy definition: {source}")

        # Ensure strategy dir exists
        sdir = self.um.strategy_dir(username)
        sdir.mkdir(parents=True, exist_ok=True)
        dest = sdir / source.name
        shutil.copy2(source, dest)

        # Atomic registry update under lock (load+save without nested locking)
        with file_lock(self._lock_path(username)):
            reg = self._load_registry_unlocked(username)
            reg[name] = {
                "file": source.name,
                "active": is_active,
                "source": str(source.resolve()),
            }
            self._save_registry_unlocked(username, reg)
        return dest

    def list(self, username: str) -> list[dict]:
        """List all strategies for a user."""
        reg = self._load_registry(username)
        result = []
        for alias, info in reg.items():
            result.append({
                "name": alias,
                "file": info["file"],
                "active": info["active"],
                "source": info.get("source", ""),
            })
        return result

    def get_active(self, username: str) -> list[tuple[str, Path]]:
        """Return (name, path) for all active strategies."""
        reg = self._load_registry(username)
        active = []
        for alias, info in reg.items():
            if info.get("active", False):
                path = self.um.strategy_dir(username) / info["file"]
                if path.exists():
                    active.append((alias, path))
        return active

    def activate(self, username: str, name: str) -> None:
        with file_lock(self._lock_path(username)):
            reg = self._load_registry_unlocked(username)
            if name not in reg:
                raise KeyError(f"Strategy '{name}' not registered for user '{username}'")
            reg[name]["active"] = True
            self._save_registry_unlocked(username, reg)

    def deactivate(self, username: str, name: str) -> None:
        with file_lock(self._lock_path(username)):
            reg = self._load_registry_unlocked(username)
            if name not in reg:
                raise KeyError(f"Strategy '{name}' not registered for user '{username}'")
            reg[name]["active"] = False
            self._save_registry_unlocked(username, reg)

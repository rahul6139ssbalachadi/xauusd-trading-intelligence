"""User account management for RK Trade.

Simple JSON-based user store — no external auth, no database server.
Each user gets a directory with:
  users/<username>/profile.json   — user metadata
  users/<username>/strategies/   — per-user strategy definitions
  users/<username>/journal.jsonl — per-user decision journal

This is account METADATA only. It does NOT authenticate against MT5.
The MT5 account (demo, login 345982869) is shared — this platform
provides analysis for whoever is running it, and user accounts segment
the strategy configurations + journals.
"""
from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from rk_trade import USERS_DIR


@dataclass
class User:
    username: str
    created_at: str
    api_key: str  # simple key for CLI auth/no-collision
    default_symbol: str = "XAUUSD"
    default_timeframe: str = "M5"
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "User":
        return cls(**d)


class UserManager:
    """Manage user accounts in a local JSON directory store."""

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = Path(base_dir) if base_dir else USERS_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _user_dir(self, username: str) -> Path:
        # sanitize: only alphanumeric + underscore
        safe = "".join(c for c in username if c.isalnum() or c in "_-")
        if not safe or safe != username:
            raise ValueError(f"Invalid username: {username!r}")
        return self.base_dir / safe

    def register(self, username: str, *, notes: str = "",
                 default_symbol: str = "XAUUSD") -> User:
        """Create a new user account. Raises if the user exists."""
        udir = self._user_dir(username)
        if udir.exists():
            raise FileExistsError(f"User already exists: {username}")
        udir.mkdir(parents=True, exist_ok=True)
        (udir / "strategies").mkdir(parents=True, exist_ok=True)

        user = User(
            username=username,
            created_at=datetime.now(timezone.utc).isoformat(),
            api_key=secrets.token_hex(16),
            default_symbol=default_symbol,
            notes=notes,
        )
        from rk_trade.utils import atomic_write_json
        atomic_write_json(udir / "profile.json", user.to_dict())
        (udir / "journal.jsonl").touch()  # create empty journal
        return user

    def get(self, username: str) -> User | None:
        """Load a user by name. Returns None if not found."""
        udir = self._user_dir(username)
        profile = udir / "profile.json"
        if not profile.exists():
            return None
        return User.from_dict(json.loads(profile.read_text()))

    def list_users(self) -> list[User]:
        """List all registered users."""
        users = []
        for udir in self.base_dir.iterdir():
            profile = udir / "profile.json"
            if profile.exists():
                try:
                    users.append(User.from_dict(json.loads(profile.read_text())))
                except Exception:
                    pass
        return users

    def delete(self, username: str) -> bool:
        """Delete a user account. Returns True if deleted."""
        import shutil
        udir = self._user_dir(username)
        if not udir.exists():
            return False
        shutil.rmtree(udir)
        return True

    def strategy_dir(self, username: str) -> Path:
        """Return the path to this user's strategy definitions."""
        return self._user_dir(username) / "strategies"

    def journal_path(self, username: str) -> Path:
        """Return the path to this user's journal."""
        return self._user_dir(username) / "journal.jsonl"

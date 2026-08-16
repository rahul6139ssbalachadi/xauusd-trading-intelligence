from __future__ import annotations

import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"


def _load_toml(name: str) -> dict:
    path = CONFIG_DIR / name
    with path.open("rb") as f:
        return tomllib.load(f)


def load_settings() -> dict:
    return _load_toml("settings.toml")


def load_symbols() -> dict:
    return _load_toml("symbols.toml")


def load_risk_limits() -> dict:
    return _load_toml("risk_limits.toml")


def load_mt5_config() -> dict:
    """Load config/mt5.toml. Git-ignored -- raises FileNotFoundError
    with a clear message if it hasn't been created yet, rather than a
    bare tomllib traceback."""
    path = CONFIG_DIR / "mt5.toml"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. This file is git-ignored and must be "
            f"created locally with terminal_path, allowed_login, and "
            f"symbol before MT5Provider can be used."
        )
    return _load_toml("mt5.toml")

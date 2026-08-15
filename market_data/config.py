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

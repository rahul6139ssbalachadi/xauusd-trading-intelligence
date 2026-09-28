from __future__ import annotations

import os
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"

# Environment variable -> config/mt5.toml key. The TOML file is git-ignored
# and therefore absent from every fresh clone and every container image, so
# these variables are the deployment path. The TOML file still wins when
# present, which keeps every existing local workflow byte-identical.
_MT5_ENV_MAP = {
    "terminal_path": "MT5_TERMINAL_PATH",
    "allowed_login": "MT5_LOGIN",
    "symbol": "BROKER_SYMBOL",
    "server": "MT5_SERVER",
}


def _env_mt5_overrides() -> dict:
    out: dict = {}
    for key, var in _MT5_ENV_MAP.items():
        val = (os.environ.get(var) or "").strip()
        if not val:
            continue
        if key == "allowed_login":
            try:
                out[key] = int(val)
            except ValueError:
                # A non-numeric login is a configuration error, not a reason
                # to fall through to a default that would not match the
                # account anyway. Keep it as a string so connect() refuses
                # loudly instead of silently comparing to 0.
                out[key] = val
        else:
            out[key] = val
    return out


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
    """Load the MT5 connection config.

    Precedence: config/mt5.toml values, then overridden by MT5_* environment
    variables. config/mt5.toml is git-ignored, so a container or a fresh
    clone that only sets environment variables still works.

    Raises FileNotFoundError with a clear message when neither source
    provides a terminal path, rather than a bare tomllib traceback.
    """
    path = CONFIG_DIR / "mt5.toml"
    if path.exists():
        cfg = _load_toml("mt5.toml")
    else:
        cfg = {}
    cfg.update(_env_mt5_overrides())
    if not cfg.get("terminal_path"):
        raise FileNotFoundError(
            f"{path} not found and MT5_TERMINAL_PATH is unset. This file is "
            f"git-ignored and must be created locally with terminal_path, "
            f"allowed_login, and symbol before MT5Provider can be used -- OR "
            f"set the environment variables MT5_TERMINAL_PATH, MT5_LOGIN and "
            f"BROKER_SYMBOL. See .env.example."
        )
    return cfg

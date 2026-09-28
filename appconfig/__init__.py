"""Environment-driven configuration for deployment.

WHY THIS EXISTS
    The project was configured through three TOML files, one of which
    (config/mt5.toml) is git-ignored and therefore absent from every fresh
    clone and every container image. That made a server deployment
    impossible without hand-editing files on the target.

    This module is the single place where environment variables are read.
    It deliberately has NO third-party dependencies: adding pydantic-settings
    here would be reasonable but would make this module unimportable in the
    minimal backtest-only image, and the parsing is trivial.

RULES OF THIS MODULE
    1. Every value has a SAFE default. Importing this module never raises
       and never requires an env file to exist.
    2. TRADING_MODE defaults to "demo".
    3. LIVE_TRADING_ENABLED defaults to FALSE, and is read with INVERTED
       polarity to match config/settings.toml: true means "block execution",
       because this build can never trade real money. Keep that invariant.
    4. Nothing here prints a secret. redact() is used by the logging setup.

PRECEDENCE
    Environment variable  >  config/*.toml  >  built-in default.
    Only market_data/config.py's mt5 loader is overridden (see that file);
    every other loader is untouched so existing behaviour is preserved.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# Repository root: this file lives at <root>/appconfig/__init__.py
ROOT = Path(__file__).resolve().parents[1]

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(RuntimeError):
    """Raised when a production-hardening check fails. Never raised on import."""


def env_str(name: str, default: str = "") -> str:
    """Read a string, stripping surrounding whitespace. Empty string if unset."""
    return (os.environ.get(name) or "").strip() or default


def env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean. Unrecognised values fall back to `default`."""
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


def env_int(name: str, default: int) -> int:
    try:
        return int(env_str(name) or default)
    except (TypeError, ValueError):
        return default


def env_path(name: str, default: str | Path) -> Path:
    return Path(env_str(name) or default).expanduser()


# ---------------------------------------------------------------------------
# The development secret. Kept here (not inline at the use site) so the
# production check below has exactly one thing to compare against, and so the
# dashboard/API can refuse to boot with it in a production environment.
# ---------------------------------------------------------------------------
DEV_SECRET_FALLBACK = "dev-secret-change-in-production"


@dataclass(frozen=True)
class AppConfig:
    """Immutable snapshot of the deployment configuration."""

    # -- mode / safety ----------------------------------------------------
    trading_mode: str = "demo"          # demo | live  (live is refused below)
    demo_execution_enabled: bool = True
    live_trading_enabled: bool = False  # INVERTED: true BLOCKS execution

    # -- api --------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_secret_key: str = DEV_SECRET_FALLBACK
    token_expire_minutes: int = 30
    allowed_origins: tuple[str, ...] = ()   # empty = same-origin only
    environment: str = "development"         # development | production

    # -- database ---------------------------------------------------------
    database_url: str = ""                    # postgres:// or sqlite path
    trading_db_path: Path = ROOT / "db" / "trading.db"
    backtests_db_path: Path = ROOT / "db" / "backtests.db"

    # -- execution adapter ------------------------------------------------
    execution_adapter: str = "null"          # null | demo | mt5 | bridge
    broker_symbol: str = "GOLD.i#"
    mt5_login: str = ""
    mt5_password: str = ""                   # never logged
    mt5_server: str = ""
    mt5_terminal_path: str = ""
    bridge_url: str = ""
    bridge_token: str = ""                   # never logged
    bridge_timeout: int = 15

    # -- notifications ----------------------------------------------------
    telegram_bridge_script: Path = Path(r"D:\rahul_ai\hermes\hermes_telegram_bridge.py")
    telegram_enabled: bool = False

    # -- observability ----------------------------------------------------
    log_level: str = "INFO"
    log_format: str = "text"                 # text | json
    log_file: str = ""                       # empty = stdout only
    health_stale_after_hours: int = 48

    # -- extra ------------------------------------------------------------
    extra: dict = field(default_factory=dict, repr=False, compare=False)

    # ------------------------------------------------------------------
    @property
    def is_live_mode(self) -> bool:
        return self.trading_mode.lower() == "live"

    @property
    def sqlite_url(self) -> str:
        return f"sqlite:///{self.trading_db_path}"

    @property
    def effective_db_path(self) -> Path:
        """Where the SQLite file actually lives, honouring DATABASE_URL."""
        url = self.database_url.strip()
        if url.startswith("sqlite:///"):
            return Path(url[len("sqlite:///"):])
        if url and not url.startswith("sqlite:"):
            # A non-SQLite DATABASE_URL is recognised but not yet implemented
            # for the research store. Callers that need SQLite use
            # trading_db_path, so this degrades safely rather than misrouting.
            return self.trading_db_path
        return self.trading_db_path

    @property
    def safe_mode_label(self) -> str:
        return "LIVE" if self.is_live_mode else "DEMO"

    def as_public_dict(self) -> dict:
        """Safe-to-serve-to-a-phone dict. Contains NO secrets.

        This is the only shape the API is allowed to expose for
        configuration, and `test_no_secrets_leak` asserts it stays clean.
        """
        return {
            "trading_mode": self.safe_mode_label,
            "demo_execution_enabled": self.demo_execution_enabled,
            "live_trading_enabled": self.live_trading_enabled,
            "live_trading_blocked": self.live_trading_enabled,
            "execution_adapter": self.execution_adapter,
            "broker_symbol": self.broker_symbol,
            "environment": self.environment,
            "log_level": self.log_level,
            "api_version": "1.0.0",
        }


_SECRET_KEYS = {
    "api_secret_key", "mt5_password", "mt5_login", "bridge_token",
}


def redact(cfg: AppConfig) -> dict:
    """Full config as a dict with every secret replaced by a marker.

    For logs and crash dumps only. `***` for unset, `***set***` for present,
    so a log line never reveals whether a secret exists beyond what the
    operator already knows.
    """
    out: dict = {}
    for name in cfg.__dataclass_fields__:
        if name in _SECRET_KEYS:
            val = getattr(cfg, name)
            out[name] = "***set***" if val else "***"
        else:
            val = getattr(cfg, name)
            out[name] = str(val) if isinstance(val, Path) else val
    return out


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _parse_origins(raw: str) -> tuple[str, ...]:
    return tuple(o.strip() for o in raw.split(",") if o.strip())


def load_config(**overrides) -> AppConfig:
    """Build an AppConfig from the environment, with keyword overrides.

    Keyword overrides exist so tests can construct a specific configuration
    without mutating os.environ globally.
    """
    mode = env_str("TRADING_MODE", "demo").lower()
    if mode not in ("demo", "live"):
        # A typo must not silently become "live". Fail closed to demo.
        mode = "demo"

    values = dict(
        trading_mode=mode,
        demo_execution_enabled=env_bool("DEMO_EXECUTION_ENABLED", True),
        # INVERTED polarity, documented in config/settings.toml.
        live_trading_enabled=env_bool("LIVE_TRADING_ENABLED", False),

        api_host=env_str("API_HOST", "0.0.0.0"),
        api_port=env_int("API_PORT", 8000),
        api_secret_key=env_str("API_SECRET_KEY", DEV_SECRET_FALLBACK),
        token_expire_minutes=env_int("TOKEN_EXPIRE_MINUTES", 30),
        allowed_origins=_parse_origins(env_str("ALLOWED_ORIGINS", "")),
        environment=env_str("ENVIRONMENT", "development").lower(),

        database_url=env_str("DATABASE_URL", ""),
        trading_db_path=env_path("TRADING_DB_PATH", ROOT / "db" / "trading.db"),
        backtests_db_path=env_path("BACKTESTS_DB_PATH", ROOT / "db" / "backtests.db"),

        execution_adapter=env_str("EXECUTION_ADAPTER", "null").lower(),
        broker_symbol=env_str("BROKER_SYMBOL", "GOLD.i#"),
        mt5_login=env_str("MT5_LOGIN", ""),
        mt5_password=env_str("MT5_PASSWORD", ""),
        mt5_server=env_str("MT5_SERVER", ""),
        mt5_terminal_path=env_str("MT5_TERMINAL_PATH", ""),
        bridge_url=env_str("BRIDGE_URL", ""),
        bridge_token=env_str("BRIDGE_TOKEN", ""),
        bridge_timeout=env_int("BRIDGE_TIMEOUT", 15),

        telegram_bridge_script=env_path(
            "TELEGRAM_BRIDGE_SCRIPT",
            r"D:\rahul_ai\hermes\hermes_telegram_bridge.py",
        ),
        telegram_enabled=env_bool("TELEGRAM_ENABLED", False),

        log_level=env_str("LOG_LEVEL", "INFO").upper(),
        log_format=env_str("LOG_FORMAT", "text").lower(),
        log_file=env_str("LOG_FILE", ""),
        health_stale_after_hours=env_int("HEALTH_STALE_AFTER_HOURS", 48),
    )
    values.update(overrides)
    return AppConfig(**values)


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Process-wide config. Cached; use load_config() for a fresh read."""
    return load_config()


def reset_config_cache() -> None:
    """Drop the cached config. Tests call this after patching os.environ."""
    get_config.cache_clear()


# ---------------------------------------------------------------------------
# Production hardening
# ---------------------------------------------------------------------------
def check_production_readiness(cfg: AppConfig) -> list[str]:
    """Return a list of blocking problems. Empty list == safe to serve.

    Called by the API at startup when ENVIRONMENT=production. Raising is the
    caller's decision; this function only reports, so the phone dashboard can
    render the same list.
    """
    problems: list[str] = []

    if cfg.api_secret_key == DEV_SECRET_FALLBACK:
        problems.append(
            "API_SECRET_KEY is still the shipped development default. "
            "Generate one with: python -c \"import secrets;"
            " print(secrets.token_urlsafe(48))\""
        )
    if len(cfg.api_secret_key) < 32:
        problems.append("API_SECRET_KEY is shorter than 32 characters.")
    if cfg.is_live_mode:
        # This build is demo-only by hard rule. Refusing here is the last
        # line of defence; the MT5 gateway refuses REAL accounts independently.
        problems.append(
            "TRADING_MODE=live is not supported by this build. "
            "The MT5 gateway refuses non-DEMO accounts by design."
        )
    if cfg.live_trading_enabled:
        problems.append(
            "LIVE_TRADING_ENABLED=true blocks execution. Invert it to false."
        )
    if cfg.execution_adapter == "mt5" and not cfg.mt5_terminal_path:
        problems.append(
            "EXECUTION_ADAPTER=mt5 requires MT5_TERMINAL_PATH."
        )
    if cfg.execution_adapter == "bridge" and not cfg.bridge_url:
        problems.append("EXECUTION_ADAPTER=bridge requires BRIDGE_URL.")
    if cfg.execution_adapter == "bridge" and not cfg.bridge_token:
        problems.append(
            "EXECUTION_ADAPTER=bridge requires BRIDGE_TOKEN. An unauthenticated "
            "order bridge must never be reachable."
        )
    if cfg.environment == "production" and not cfg.allowed_origins:
        # Not a hard error: same-origin is the secure default and is what a
        # reverse-proxied deployment wants. Recorded so an operator who
        # expected a split origin knows why nothing is allowed.
        pass
    return problems


def require_production_readiness(cfg: AppConfig) -> None:
    """Raise ConfigError if the deployment is unsafe. Use in app startup."""
    problems = check_production_readiness(cfg)
    if problems:
        raise ConfigError(
            "Refusing to start in production mode:\n  - "
            + "\n  - ".join(problems)
        )


__all__ = [
    "AppConfig", "ConfigError", "DEV_SECRET_FALLBACK", "ROOT",
    "check_production_readiness", "env_bool", "env_int", "env_path",
    "env_str", "get_config", "load_config", "redact", "require_production_readiness",
    "reset_config_cache",
]

"""Phase 1: FastAPI Auth + Trading API — self-improved with proper async DB handling"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, field_validator
from collections import defaultdict
import time

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Server-ready settings, read from the environment with safe defaults so a
# container starts with no .env at all. See appconfig/__init__.py and
# .env.example. The dev default is retained for local development ONLY;
# require_production_readiness() refuses it when ENVIRONMENT=production.
from appconfig import get_config, require_production_readiness, ConfigError

_app_cfg = get_config()

# Backwards-compatible module-level names. Existing code and tests import
# SECRET_KEY / ALGORITHM / ALLOWED_ORIGINS from here.
SECRET_KEY = os.getenv("TRADING_SECRET_KEY") or _app_cfg.api_secret_key
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = _app_cfg.token_expire_minutes

# CORS: same-origin only by default. The previous default was "*", which is
# not merely loose but invalid when paired with allow_credentials=True —
# browsers reject the combination outright, so a split-origin deployment
# silently failed. Empty ALLOWED_ORIGINS now means "no cross-origin access",
# which is the correct posture behind a reverse proxy that serves the
# dashboard and the API from one host. Widen deliberately, never with "*".
ALLOWED_ORIGINS = list(_app_cfg.allowed_origins)

# Rate limiting
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 5
# Cap the stored windows so a long-lived process cannot grow this dict
# without bound. An IP that stays at the limit is simply forgotten.
RATE_LIMIT_MAX_TRACKED_IPS = 4096
rate_limit_store: dict[str, list[float]] = defaultdict(list)


def check_rate_limit(client_ip: str) -> None:
    now = time.time()
    window = rate_limit_store[client_ip]
    rate_limit_store[client_ip] = [t for t in window if now - t < RATE_LIMIT_WINDOW]
    if len(rate_limit_store[client_ip]) >= RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="Too many requests")
    if len(rate_limit_store) > RATE_LIMIT_MAX_TRACKED_IPS:
        # Evict the least-recently-active entries rather than refusing
        # service to a new client.
        for ip in sorted(rate_limit_store, key=lambda k: rate_limit_store[k][-1] if rate_limit_store[k] else 0)[: len(rate_limit_store) - RATE_LIMIT_MAX_TRACKED_IPS // 2]:
            rate_limit_store.pop(ip, None)
    rate_limit_store[client_ip].append(now)


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

# Repository root, resolved from this file rather than from the working
# directory. The dashboards used to be served via a relative
# FileResponse("reports/..."), which 500s whenever the process is started
# from anywhere other than the repo root -- i.e. always, under Docker,
# systemd, or uWSGI.
REPO_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = REPO_ROOT / "reports"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup/shutdown.

    In production this refuses to serve with a dev-default API secret, an
    unsupported live mode, or a blocked-execution misconfiguration. In
    development it only logs the problems, so local work is never gated.
    """
    from observability import setup_logging
    setup_logging()

    from appconfig import check_production_readiness
    cfg = get_config()
    problems = check_production_readiness(cfg)
    if cfg.environment == "production":
        if problems:
            msg = "Refusing to start in production:\n  - " + "\n  - ".join(problems)
            raise ConfigError(msg)
        logger.info("production startup OK: %s", cfg.as_public_dict())
    else:
        logger.warning(
            "development startup. Production-readiness problems (%d): %s",
            len(problems), "; ".join(problems) or "none",
            extra={"extra_data": {"problems": problems}},
        )
    logger.info("mode=%s live_enabled=%s adapter=%s",
                cfg.safe_mode_label, cfg.live_trading_enabled,
                cfg.execution_adapter)
    yield
    logger.info("shutdown complete")


app = FastAPI(title="Trading Intelligence API", version="1.0.0",
              lifespan=_lifespan)

# Same-origin only unless explicitly widened. See ALLOWED_ORIGINS above.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logger = logging.getLogger("api.main")


# ---------------------------------------------------------------------------
# DB helpers - synchronous context manager for sync SQLite
# ---------------------------------------------------------------------------
def get_db_path() -> Path:
    """Get DB path from environment or default.

    Honours TRADING_DB_PATH (used by the tests) and, failing that, the
    appconfig value, which itself resolves relative paths against the
    repository root so a container can mount the database anywhere.
    """
    env = os.getenv("TRADING_DB_PATH")
    if env:
        p = Path(env).expanduser()
        return p if p.is_absolute() else REPO_ROOT / p
    from appconfig import get_config
    return get_config().effective_db_path


@contextmanager
def get_db():
    """Synchronous database connection context manager."""
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    user_id: int
    email: str
    role: str
    client_id: int | None = None


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    role: str = "client"
    client_id: int | None = None

    @field_validator("password")
    @classmethod
    def password_strength(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v

    @field_validator("role")
    @classmethod
    def role_valid(cls, v):
        if v not in ("client", "admin"):
            raise ValueError("Role must be 'client' or 'admin'")
        return v


class UserOut(BaseModel):
    id: int
    email: str
    role: str
    client_id: int | None = None


class TradeOut(BaseModel):
    id: int
    symbol: str
    direction: str
    entry_price: float
    exit_price: float | None
    stop_loss: float | None
    take_profit: float | None
    lots: float
    pnl_pips: float | None
    pnl_usd: float | None
    strategy: str | None
    version: str | None
    entry_time: str | None
    exit_time: str | None
    status: str


class EquityPoint(BaseModel):
    date: str
    equity: float


class SummaryOut(BaseModel):
    total_trades: int
    win_rate: float
    profit_factor: float
    net_pnl_usd: float
    max_drawdown_pct: float
    current_strategy: str


class ClientOut(BaseModel):
    id: int
    name: str
    total_trades: int
    net_pnl_usd: float
    win_rate: float


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------
def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


async def get_current_user(token: str = Depends(oauth2_scheme)) -> TokenData:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        email = payload.get("email")
        role = payload.get("role")
        client_id = payload.get("client_id")
        if user_id is None or email is None or role is None:
            raise credentials_exception
        return TokenData(user_id=int(user_id), email=email, role=role, client_id=client_id)
    except JWTError:
        raise credentials_exception


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------
@app.post("/api/auth/login", response_model=Token)
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
):
    check_rate_limit(request.client.host)
    with get_db() as conn:
        cur = conn.execute("SELECT * FROM users WHERE email = ?", (form_data.username,))
        user = cur.fetchone()

        if not user or not verify_password(form_data.password, user["password_hash"]):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect email or password",
            )

        access_token = create_access_token(
            data={
                "sub": str(user["id"]),
                "email": user["email"],
                "role": user["role"],
                "client_id": user["client_id"],
            },
            expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        )

        conn.execute(
            "UPDATE users SET last_login = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), user["id"]),
        )
        conn.commit()

    return Token(access_token=access_token)


@app.post("/api/auth/register", response_model=UserOut)
async def register(
    user_data: UserCreate,
    current_user: TokenData = Depends(get_current_user),
):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    with get_db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (user_data.email,)).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="Email already registered")

        client_id = user_data.client_id
        if client_id is None and user_data.role == "client":
            conn.execute("INSERT INTO clients (name) VALUES (?)", (f"Client_{user_data.email}",))
            client_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        conn.execute(
            "INSERT INTO users (email, password_hash, role, client_id) VALUES (?, ?, ?, ?)",
            (user_data.email, get_password_hash(user_data.password), user_data.role, client_id),
        )
        conn.commit()

        new_user = conn.execute("SELECT * FROM users WHERE email = ?", (user_data.email,)).fetchone()

    return UserOut(
        id=new_user["id"],
        email=new_user["email"],
        role=new_user["role"],
        client_id=new_user["client_id"],
    )


@app.get("/api/auth/me", response_model=UserOut)
async def get_me(current_user: TokenData = Depends(get_current_user)):
    return UserOut(
        id=current_user.user_id,
        email=current_user.email,
        role=current_user.role,
        client_id=current_user.client_id,
    )


# ---------------------------------------------------------------------------
# Client endpoints (scoped to their client_id)
# ---------------------------------------------------------------------------
@app.get("/api/trades", response_model=list[TradeOut])
async def get_trades(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: TokenData = Depends(get_current_user),
):
    with get_db() as conn:
        if current_user.role == "admin":
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY entry_time DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades WHERE client_id = ? ORDER BY entry_time DESC LIMIT ? OFFSET ?",
                (current_user.client_id, limit, offset),
            ).fetchall()

    return [dict(r) for r in rows]


@app.get("/api/trades/{trade_id}", response_model=TradeOut)
async def get_trade(
    trade_id: int,
    current_user: TokenData = Depends(get_current_user),
):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Trade not found")

    trade = dict(row)
    if current_user.role != "admin" and trade.get("client_id") != current_user.client_id:
        raise HTTPException(status_code=403, detail="Access denied")

    return trade


@app.get("/api/equity", response_model=list[EquityPoint])
async def get_equity(
    days: int = Query(90, ge=1, le=365),
    current_user: TokenData = Depends(get_current_user),
):
    with get_db() as conn:
        if current_user.role == "admin":
            rows = conn.execute(
                "SELECT date, equity FROM equity_curve ORDER BY date DESC LIMIT ?",
                (days,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT date, equity FROM equity_curve WHERE client_id = ? ORDER BY date DESC LIMIT ?",
                (current_user.client_id, days),
            ).fetchall()

    return [{"date": r["date"], "equity": r["equity"]} for r in rows]


@app.get("/api/summary", response_model=SummaryOut)
async def get_summary(current_user: TokenData = Depends(get_current_user)):
    with get_db() as conn:
        if current_user.role == "admin":
            row = conn.execute("SELECT * FROM performance_summary LIMIT 1").fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM performance_summary WHERE client_id = ?",
                (current_user.client_id,),
            ).fetchone()

    if not row:
        return SummaryOut(
            total_trades=0, win_rate=0.0, profit_factor=0.0,
            net_pnl_usd=0.0, max_drawdown_pct=0.0, current_strategy="N/A",
        )

    return SummaryOut(
        total_trades=row["total_trades"],
        win_rate=row["win_rate"],
        profit_factor=row["profit_factor"],
        net_pnl_usd=row["net_pnl_usd"],
        max_drawdown_pct=row["max_drawdown_pct"],
        current_strategy=row["current_strategy"],
    )


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------
@app.get("/api/admin/clients", response_model=list[ClientOut])
async def list_clients(current_user: TokenData = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    with get_db() as conn:
        rows = conn.execute("""
            SELECT c.id, c.name,
                   COUNT(t.id) as total_trades,
                   COALESCE(SUM(t.pnl_usd), 0) as net_pnl_usd,
                   COALESCE(AVG(CASE WHEN t.pnl_pips > 0 THEN 1.0 ELSE 0.0 END), 0) as win_rate
            FROM clients c
            LEFT JOIN trades t ON t.client_id = c.id
            GROUP BY c.id
            ORDER BY c.name
        """).fetchall()

    return [ClientOut(
        id=r["id"], name=r["name"], total_trades=r["total_trades"],
        net_pnl_usd=r["net_pnl_usd"], win_rate=r["win_rate"],
    ) for r in rows]


@app.get("/api/admin/clients/{client_id}/trades", response_model=list[TradeOut])
async def get_client_trades(
    client_id: int,
    limit: int = Query(50, ge=1, le=500),
    current_user: TokenData = Depends(get_current_user),
):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE client_id = ? ORDER BY entry_time DESC LIMIT ?",
            (client_id, limit),
        ).fetchall()

    return [dict(r) for r in rows]


@app.get("/api/admin/risk_state")
async def get_risk_state(current_user: TokenData = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM risk_state ORDER BY timestamp DESC LIMIT 10"
        ).fetchall()

    return [dict(r) for r in rows]


from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

# Serve dashboard. Paths are absolute (derived from REPO_ROOT) so the API
# works from any working directory -- see the note at the top of this file.
@app.get("/")
@app.get("/dashboard")
async def serve_dashboard():
    # The owner's primary screen. Falls back to the client dashboard if
    # the main one is ever removed, rather than 404ing.
    main = REPO_ROOT / "dashboard" / "index.html"
    if main.exists():
        return FileResponse(main, media_type="text/html",
                            headers={"Cache-Control": "no-store"})
    path = REPORTS_DIR / "client_dashboard.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="client dashboard not built")
    return FileResponse(path, media_type="text/html")

@app.get("/admin")
async def serve_admin():
    path = REPORTS_DIR / "dashboard.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="admin dashboard not built")
    return FileResponse(path, media_type="text/html")


@app.get("/m")
async def serve_mobile():
    """Phone dashboard (Phase 5). Single file, no build step, no CDN.

    The API base is derived from window.location.origin inside the page, so
    the same file works at localhost, at a LAN IP, and behind a reverse
    proxy on a domain with no rebuild.
    """
    path = REPO_ROOT / "dashboard" / "mobile.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="mobile dashboard missing")
    return FileResponse(path, media_type="text/html",
                        headers={"Cache-Control": "no-cache"})


# ----------------------------------------------------------------------
# Live status (read-only, no auth).
#
# Deliberately unauthenticated: the payload contains no credentials and
# no order capability, and the phone dashboard needs it before login.
# It exposes only market state and the system's own decisions. Do NOT
# mount anything that can place an order on this path.
# ----------------------------------------------------------------------
@app.get("/api/live")
async def get_live_status():
    """Reader health + latest signals, for the dashboard."""
    from api.live_status import status
    return status()


@app.get("/api/live/health")
async def get_live_health():
    """Just the read-service health. Cheap enough to poll every few
    seconds, and the staleness of the data is the single most important
    thing to surface."""
    from api.live_status import read_health
    return read_health()


@app.get("/live")
async def serve_live_dashboard():
    """Live status dashboard: MT5 connection, positions, latest signals.

    Single file, no build step, no CDN, no auth — it renders only
    read-only state.
    """
    path = REPO_ROOT / "dashboard" / "live.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="live dashboard missing")
    return FileResponse(path, media_type="text/html",
                        headers={"Cache-Control": "no-store"})


# ----------------------------------------------------------------------
# Main dashboard (the owner's requested overview layout).
# ----------------------------------------------------------------------
@app.get("/api/dashboard")
async def get_dashboard():
    """Header, money, positions, recent activity, strategy cards."""
    from api.dashboard_api import overview
    return overview()


@app.get("/api/dashboard/strategy/{version}")
async def get_strategy_page(version: str):
    """Per-strategy page: trades, win rate, PF, max DD, average trade,
    plus the robustness evidence (Monte Carlo, walk-forward)."""
    from api.dashboard_api import strategy_page
    d = strategy_page(version)
    if not d.get("found"):
        raise HTTPException(status_code=404,
                            detail=f"strategy {version} not found")
    return d


# ---------------------------------------------------------------------------
# Deployment API (Phase 4): /health, /api/status, /api/system, /api/strategies,
# /api/signals, /api/performance, /api/backtests, /api/worker, /api/logs,
# /api/errors, /api/control/*, and the /ws status channel.
#
# Mounted AFTER the legacy endpoints above so the original client-scoped
# /api/trades keeps precedence. See api/deploy.py for the full list.
# ---------------------------------------------------------------------------
from api.deploy import ROUTER as _deploy_router
app.include_router(_deploy_router)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
# NOTE: /api/health is kept for backwards compatibility. The richer,
# deployment-oriented endpoint is GET /health (from api/deploy.py), which
# is what Docker's healthcheck and any load balancer should call.
@app.get("/api/health")
async def health():
    from appconfig import get_config as _gc
    return {"status": "ok", "version": "1.0.0", "mode": _gc().safe_mode_label}


if __name__ == "__main__":
    import uvicorn
    from appconfig import get_config as _c
    _cfg = _c()
    uvicorn.run("api.main:app", host=_cfg.api_host, port=_cfg.api_port,
                reload=False)

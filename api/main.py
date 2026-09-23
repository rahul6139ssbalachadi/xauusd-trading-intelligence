"""Phase 1: FastAPI Auth + Trading API with JWT"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SECRET_KEY = "your-secret-key-change-in-production"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

app = FastAPI(title="Trading Intelligence API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = Path(__file__).resolve().parents[1] / "db" / "trading.db"


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
    email: str
    password: str
    role: str = "client"
    client_id: int | None = None


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
# DB helpers
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


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
        return TokenData(user_id=user_id, email=email, role=role, client_id=client_id)
    except JWTError:
        raise credentials_exception


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------
@app.post("/api/auth/login", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    conn = next(get_db())
    cur = conn.execute("SELECT * FROM users WHERE email = ?", (form_data.username,))
    user = cur.fetchone()

    if not user or not verify_password(form_data.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    access_token = create_access_token(
        data={
            "sub": user["id"],
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

    conn = next(get_db())
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
    conn.close()
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
    conn = next(get_db())

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

    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/trades/{trade_id}", response_model=TradeOut)
async def get_trade(
    trade_id: int,
    current_user: TokenData = Depends(get_current_user),
):
    conn = next(get_db())
    row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    conn.close()

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
    conn = next(get_db())

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

    conn.close()
    return [{"date": r["date"], "equity": r["equity"]} for r in rows]


@app.get("/api/summary", response_model=SummaryOut)
async def get_summary(current_user: TokenData = Depends(get_current_user)):
    conn = next(get_db())

    if current_user.role == "admin":
        row = conn.execute("SELECT * FROM performance_summary LIMIT 1").fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM performance_summary WHERE client_id = ?",
            (current_user.client_id,),
        ).fetchone()

    conn.close()

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

    conn = next(get_db())
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
    conn.close()

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

    conn = next(get_db())
    rows = conn.execute(
        "SELECT * FROM trades WHERE client_id = ? ORDER BY entry_time DESC LIMIT ?",
        (client_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/admin/risk_state")
async def get_risk_state(current_user: TokenData = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    conn = next(get_db())
    rows = conn.execute(
        "SELECT * FROM risk_state ORDER BY timestamp DESC LIMIT 10"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)

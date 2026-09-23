"""API tests for auth and trading endpoints."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def test_env(tmp_path, monkeypatch):
    """Set up test database and environment."""
    db_path = tmp_path / "test_trading.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE clients (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('client', 'admin')), client_id INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_login TIMESTAMP, FOREIGN KEY (client_id) REFERENCES clients(id));
        CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER NOT NULL, symbol TEXT NOT NULL, direction TEXT NOT NULL, entry_price REAL NOT NULL, exit_price REAL, stop_loss REAL, take_profit REAL, lots REAL NOT NULL DEFAULT 0.01, pnl_pips REAL, pnl_usd REAL, strategy TEXT, version TEXT, entry_time TIMESTAMP, exit_time TIMESTAMP, status TEXT DEFAULT 'open', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (client_id) REFERENCES clients(id));
        CREATE TABLE equity_curve (id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER NOT NULL, date TEXT NOT NULL, equity REAL NOT NULL, FOREIGN KEY (client_id) REFERENCES clients(id));
        CREATE TABLE performance_summary (id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER NOT NULL UNIQUE, total_trades INTEGER DEFAULT 0, win_rate REAL DEFAULT 0.0, profit_factor REAL DEFAULT 0.0, net_pnl_usd REAL DEFAULT 0.0, max_drawdown_pct REAL DEFAULT 0.0, current_strategy TEXT DEFAULT 'N/A', updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (client_id) REFERENCES clients(id));
        CREATE TABLE risk_state (id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER NOT NULL, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, daily_pnl_usd REAL DEFAULT 0.0, weekly_pnl_usd REAL DEFAULT 0.0, consecutive_losses INTEGER DEFAULT 0, kill_switch_active BOOLEAN DEFAULT 0, FOREIGN KEY (client_id) REFERENCES clients(id));
    """)
    from passlib.context import CryptContext
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    admin_hash = pwd_context.hash("admin123")
    client_hash = pwd_context.hash("client123")
    conn.execute("INSERT INTO clients (id, name) VALUES (1, 'Test Client')")
    conn.execute("INSERT INTO users (email, password_hash, role, client_id) VALUES (?, ?, 'admin', NULL)", ("admin@test.com", admin_hash))
    conn.execute("INSERT INTO users (email, password_hash, role, client_id) VALUES (?, ?, 'client', 1)", ("client@test.com", client_hash))
    conn.commit()
    conn.close()
    
    # Set env var and disable rate limiting
    monkeypatch.setenv("TRADING_DB_PATH", str(db_path))
    monkeypatch.setattr("api.main.check_rate_limit", lambda ip: None)
    
    yield db_path


@pytest.fixture
def client(test_env):
    """Create test client."""
    from fastapi.testclient import TestClient
    from api.main import app
    with TestClient(app) as c:
        yield c


class TestAuth:
    def test_login_success_admin(self, client):
        response = client.post("/api/auth/login", data={"username": "admin@test.com", "password": "admin123"})
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_login_success_client(self, client):
        response = client.post("/api/auth/login", data={"username": "client@test.com", "password": "client123"})
        assert response.status_code == 200
        assert "access_token" in response.json()

    def test_login_wrong_password(self, client):
        response = client.post("/api/auth/login", data={"username": "admin@test.com", "password": "wrongpassword"})
        assert response.status_code == 401

    def test_login_nonexistent_user(self, client):
        response = client.post("/api/auth/login", data={"username": "nobody@test.com", "password": "password123"})
        assert response.status_code == 401

    def test_get_me_authenticated(self, client):
        login_resp = client.post("/api/auth/login", data={"username": "admin@test.com", "password": "admin123"})
        token = login_resp.json()["access_token"]
        response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        data = response.json()
        assert data["email"] == "admin@test.com"
        assert data["role"] == "admin"

    def test_get_me_no_token(self, client):
        response = client.get("/api/auth/me")
        assert response.status_code == 401

    def test_get_me_invalid_token(self, client):
        response = client.get("/api/auth/me", headers={"Authorization": "Bearer invalid.token.here"})
        assert response.status_code == 401

    def test_register_admin_only(self, client):
        login_resp = client.post("/api/auth/login", data={"username": "admin@test.com", "password": "admin123"})
        token = login_resp.json()["access_token"]
        response = client.post("/api/auth/register",
            json={"email": "newuser@test.com", "password": "newpass123", "role": "client"},
            headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        assert response.json()["email"] == "newuser@test.com"


class TestHealth:
    def test_health_check(self, client):
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestTradingEndpoints:
    def test_get_trades_client_scoped(self, client):
        login_resp = client.post("/api/auth/login", data={"username": "client@test.com", "password": "client123"})
        token = login_resp.json()["access_token"]
        response = client.get("/api/trades", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200

    def test_get_trades_admin_all(self, client):
        login_resp = client.post("/api/auth/login", data={"username": "admin@test.com", "password": "admin123"})
        token = login_resp.json()["access_token"]
        response = client.get("/api/trades", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200

    def test_get_summary(self, client):
        login_resp = client.post("/api/auth/login", data={"username": "client@test.com", "password": "client123"})
        token = login_resp.json()["access_token"]
        response = client.get("/api/summary", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        data = response.json()
        assert "total_trades" in data
        assert "win_rate" in data

    def test_admin_list_clients(self, client):
        login_resp = client.post("/api/auth/login", data={"username": "admin@test.com", "password": "admin123"})
        token = login_resp.json()["access_token"]
        response = client.get("/api/admin/clients", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_client_cannot_access_admin(self, client):
        login_resp = client.post("/api/auth/login", data={"username": "client@test.com", "password": "client123"})
        token = login_resp.json()["access_token"]
        response = client.get("/api/admin/clients", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403

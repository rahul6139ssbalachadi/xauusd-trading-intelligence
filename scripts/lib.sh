#!/usr/bin/env bash
# Shared helpers for the deployment scripts. Source, do not execute.
#
# Every script that uses this file is idempotent: running it twice is the
# same as running it once, and none of them destroy data.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

PROD_COMPOSE="docker compose -f docker-compose.prod.yml"

# --- output -----------------------------------------------------------------
if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_OFF=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_OFF=""
fi
ok()   { printf '%s[ok]%s   %s\n'   "$C_OK"   "$C_OFF" "$*"; }
warn() { printf '%s[warn]%s %s\n'   "$C_WARN" "$C_OFF" "$*"; }
die()  { printf '%s[fail]%s %s\n'   "$C_ERR"  "$C_OFF" "$*" >&2; exit 1; }
info() { printf '       %s\n' "$*"; }

# --- preflight ---------------------------------------------------------------
require_docker() {
  command -v docker >/dev/null 2>&1 \
    || die "docker is not installed. See docs/SERVER_DEPLOYMENT.md step 1."
  docker compose version >/dev/null 2>&1 \
    || die "docker compose v2 is not available. Install the compose plugin."
}

require_env() {
  [ -f .env ] || die ".env not found. Copy .env.example to .env and set API_SECRET_KEY."
  if grep -qE '^API_SECRET_KEY=\s*$' .env; then
    die "API_SECRET_KEY is empty in .env. Generate one:
       python3 -c \"import secrets; print(secrets.token_urlsafe(48))\""
  fi
}

# Refuse to start anything in a state that would be unsafe, unless the
# operator has explicitly acknowledged it.
assert_safe_mode() {
  local mode live
  mode="$(grep -E '^TRADING_MODE=' .env 2>/dev/null | cut -d= -f2- | tr -d '[:space:]' || true)"
  live="$(grep -E '^LIVE_TRADING_ENABLED=' .env 2>/dev/null | cut -d= -f2- | tr -d '[:space:]' || true)"
  if [ "${mode,,}" = "live" ]; then
    die "TRADING_MODE=live in .env. This build is DEMO-only and the API will
     refuse to start. Set TRADING_MODE=demo."
  fi
  if [ "${live,,}" = "true" ]; then
    warn "LIVE_TRADING_ENABLED=true in .env. This BLOCKS execution (inverted
   polarity). It should normally be false. Starting anyway."
  fi
}

# --- data --------------------------------------------------------------------
# The databases are git-ignored, so a fresh clone has none. Everything that
# needs them is useless without this, hence the check on every start.
require_db() {
  if [ ! -f db/trading.db ]; then
    warn "db/trading.db is missing (it is git-ignored by design)."
    info "The API will start but /health will report the database as down."
    info "To fix: copy the database in, or ingest on a machine with MT5:"
    info "  scp <windows-pc>:/path/to/trading.db db/trading.db"
    info "  # or, with an MT5 terminal available:"
    info "  python scripts/ingest_extended_gold.py"
  fi
}

wait_for_health() {
  local url="http://127.0.0.1:8000/health" tries="${1:-30}" i
  for i in $(seq 1 "$tries"); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      ok "API is healthy ($url)"
      curl -fsS "$url" | head -c 400; echo
      return 0
    fi
    sleep 2
  done
  die "API did not become healthy within $((tries*2))s. Check: $PROD_COMPOSE logs api"
}

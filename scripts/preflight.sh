#!/usr/bin/env bash
# One-shot check of every deployment precondition. Read-only.
#
#   ./scripts/preflight.sh
#
# Run this on a fresh server BEFORE start.sh. It reports what is missing
# rather than failing halfway through a deployment.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

echo "=== toolchain ==="
if command -v docker >/dev/null 2>&1; then
  ok "docker $(docker --version | awk '{print $3}' | tr -d ,)"
else
  die "docker is not installed (docs/SERVER_DEPLOYMENT.md step 1)"
fi
if docker compose version >/dev/null 2>&1; then
  ok "docker compose $(docker compose version --short)"
else
  die "docker compose v2 plugin is missing"
fi
command -v git >/dev/null 2>&1 && ok "git present" || warn "git not found (needed to pull updates)"
command -v curl  >/dev/null 2>&1 && ok "curl present" || warn "curl not found (healthchecks need it)"

echo
echo "=== configuration ==="
if [ -f .env ]; then
  ok ".env exists"
  if grep -qE '^API_SECRET_KEY=\s*$' .env; then
    die "API_SECRET_KEY is empty — the API will refuse to start in production"
  else
    ok "API_SECRET_KEY is set"
  fi
  if [ -z "$(grep -E '^TRADING_MODE=' .env | cut -d= -f2- | tr -d '[:space:]')" ]; then
    warn "TRADING_MODE unset — defaulting to demo (safe)"
  else
    ok "TRADING_MODE=$(grep -E '^TRADING_MODE=' .env | cut -d= -f2- | tr -d '[:space:]')"
  fi
  if [ -f .env ] && grep -qE '^LIVE_TRADING_ENABLED=\s*true' .env; then
    warn "LIVE_TRADING_ENABLED=true — this BLOCKS execution (inverted polarity)"
  else
    ok "LIVE_TRADING_ENABLED is not true"
  fi
  ADAPTER="$(grep -E '^EXECUTION_ADAPTER=' .env | cut -d= -f2- | tr -d '[:space:]' || echo null)"
  case "$ADAPTER" in
    null|demo) ok "EXECUTION_ADAPTER=$ADAPTER (no orders can be placed)" ;;
    mt5)      warn "EXECUTION_ADAPTER=mt5 — requires MetaTrader5, which is Windows-only.
   If you are on Linux, the adapter will report UNAVAILABLE and place nothing." ;;
    bridge)   ok "EXECUTION_ADAPTER=bridge — see docs/MT5_ARCHITECTURE.md" ;;
    *)        warn "EXECUTION_ADAPTER=$ADAPTER is not a known adapter; it will fall back to null" ;;
  esac
else
  die ".env missing. Run: cp .env.example .env && set API_SECRET_KEY"
fi

echo
echo "=== data ==="
if [ -f db/trading.db ]; then
  ok "db/trading.db present ($(du -h db/trading.db | cut -f1))"
else
  warn "db/trading.db missing — git-ignored by design; /health will report it down"
fi

echo
echo "=== disk ==="
AVAIL="$(df -h . | awk 'NR==2 {print $4}')"
info "available: $AVAIL"
if [ "${AVAIL%%[0-9]*}" -lt 2 ] 2>/dev/null; then
  warn "less than 2GB free"
fi

echo
ok "preflight complete"

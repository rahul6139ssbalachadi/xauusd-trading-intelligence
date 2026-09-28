#!/usr/bin/env bash
# Start the production stack. Idempotent: safe to run repeatedly.
#
#   ./scripts/start.sh
#
# Order matters and is enforced here:
#   1. docker present
#   2. .env present with a real API_SECRET_KEY
#   3. TRADING_MODE is not "live" and execution is not force-blocked
#   4. build, up, wait for health
#
# It does NOT start a reverse proxy. That is a separate, host-level concern:
# see docs/SERVER_DEPLOYMENT.md step 11 and docs/SECURITY.md.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_docker
require_env
assert_safe_mode

info "building image..."
$PROD_COMPOSE build

info "starting services..."
$PROD_COMPOSE up -d

require_db
wait_for_health 30

$PROD_COMPOSE ps
ok "stack is up"
info "  API        : http://127.0.0.1:8000"
info "  phone view : http://127.0.0.1:8000/m  (or your server IP if API_BIND is 0.0.0.0)"
info "  health     : http://127.0.0.1:8000/health"
info "  logs       : docker compose -f docker-compose.prod.yml logs -f"

#!/usr/bin/env bash
# Show what the system is actually doing. Read-only and safe to run any time.
#
#   ./scripts/status.sh
#
# Prints container state, the full /health payload, and the last decisions
# from the journal. This is the first thing to run when something looks wrong.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_docker

echo "=== containers ==="
$PROD_COMPOSE ps || true

echo
echo "=== health ==="
if curl -fsS http://127.0.0.1:8000/health 2>/dev/null; then
  echo
else
  warn "API not responding on 127.0.0.1:8000"
  info "start it with: ./scripts/start.sh"
  info "or read logs with: $PROD_COMPOSE logs --tail=50 api"
  exit 0
fi

echo
echo "=== last 5 journal decisions ==="
JOURNAL="execution/journal.jsonl"
if [ -f "$JOURNAL" ]; then
  tail -n 5 "$JOURNAL"
else
  info "no journal at $JOURNAL (the runners have not executed yet)"
fi

echo
echo "=== safety state ==="
curl -fsS http://127.0.0.1:8000/api/status 2>/dev/null \
  | tr ',' '\n' | grep -E 'mode|live_trading|adapter' || true

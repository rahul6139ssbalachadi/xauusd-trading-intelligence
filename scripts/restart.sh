#!/usr/bin/env bash
# Restart services. Idempotent.
#
#   ./scripts/restart.sh            # recreate containers, keep the image
#   ./scripts/restart.sh --rebuild  # also rebuild the image (use after code pulls)
#
# This is the normal way to apply a git pull. The database is never touched.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_docker
require_env

REBUILD=0
for arg in "$@"; do
  case "$arg" in
    --rebuild) REBUILD=1 ;;
    *) die "unknown option: $arg (only --rebuild is supported)" ;;
  esac
done

if [ "$REBUILD" -eq 1 ]; then
  info "rebuilding image..."
  $PROD_COMPOSE build
fi

info "restarting services..."
$PROD_COMPOSE up -d
wait_for_health 30
$PROD_COMPOSE ps
ok "stack restarted"

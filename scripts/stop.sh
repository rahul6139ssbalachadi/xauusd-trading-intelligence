#!/usr/bin/env bash
# Stop the stack WITHOUT deleting data. Idempotent.
#
#   ./scripts/stop.sh
#
# `docker compose stop`/`down` never removes named volumes, so the database,
# journals and logs survive. To delete those too you must say so explicitly
# with --purge, which prompts first.
#
#   ./scripts/stop.sh --purge     # removes volumes: DESTROYS the database

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_docker

PURGE=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    *) die "unknown option: $arg (only --purge is supported)" ;;
  esac
done

if [ "$PURGE" -eq 1 ]; then
  warn "--purge will DELETE the database, journals and logs."
  printf 'Type the word DELETE to confirm: '
  read -r confirm
  [ "$confirm" = "DELETE" ] || die "aborted; nothing was removed"
  $PROD_COMPOSE down -v
  ok "stack stopped and volumes removed"
else
  $PROD_COMPOSE down
  ok "stack stopped. Data volumes preserved."
  info "  to also delete the database and journals: ./scripts/stop.sh --purge"
fi

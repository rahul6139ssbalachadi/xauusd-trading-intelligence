#!/usr/bin/env bash
# Tail every log. Ctrl-C to stop; this never changes state.
#
#   ./scripts/logs.sh          # follow everything
#   ./scripts/logs.sh api      # follow just the API
#   ./scripts/logs.sh -n 200   # last 200 lines, do not follow

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_docker

LINES=100
FOLLOW=1
SERVICE=""

while [ $# -gt 0 ]; do
  case "$1" in
    -n) LINES="${2:-100}"; FOLLOW=0; shift 2 ;;
    api|worker) SERVICE="$1"; shift ;;
    *) die "usage: ./scripts/logs.sh [-n LINES] [api|worker]" ;;
  esac
done

if [ -n "$SERVICE" ]; then
  if [ "$FOLLOW" -eq 1 ]; then
    exec $PROD_COMPOSE logs -f --tail="$LINES" "$SERVICE"
  else
    exec $PROD_COMPOSE logs --tail="$LINES" "$SERVICE"
  fi
fi

if [ "$FOLLOW" -eq 1 ]; then
  exec $PROD_COMPOSE logs -f --tail="$LINES"
else
  exec $PROD_COMPOSE logs --tail="$LINES"
fi

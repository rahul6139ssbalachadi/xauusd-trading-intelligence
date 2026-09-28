#!/usr/bin/env bash
# Restore a backup. Prompts before overwriting anything.
#
#   ./scripts/restore.sh backups/trading-20260928-120000.tar.gz
#
# The stack is stopped automatically if it is running, and is NOT restarted —
# restoring underneath a live API would let it serve a half-written database.
# Start it yourself when you are satisfied.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ARCHIVE="${1:-}"
[ -n "$ARCHIVE" ] || die "usage: ./scripts/restore.sh <archive.tar.gz>"
[ -f "$ARCHIVE" ] || die "archive not found: $ARCHIVE"

tar -tzf "$ARCHIVE" >/dev/null 2>&1 || die "archive is not a valid gzip tar: $ARCHIVE"

echo "=== archive contents (top level) ==="
tar -tzf "$ARCHIVE" | awk -F/ '{print $1"/"$2}' | sort -u | head -30
echo

warn "This will OVERWRITE: db/, execution/journal.jsonl, journal/, paper/,"
warn "reports/, run/, strategy/defs/ with the archive contents."
if [ -f db/trading.db ]; then
  warn "  In particular it will replace the existing db/trading.db."
fi
printf 'Type RESTORE to continue: '
read -r confirm
[ "$confirm" = "RESTORE" ] || die "aborted; nothing was changed"

if command -v docker >/dev/null 2>&1; then
  info "stopping the stack so nothing serves a partial database"
  $PROD_COMPOSE down >/dev/null 2>&1 || true
fi

# Take a safety copy of what is being replaced, so a bad restore is itself
# recoverable. Idempotent: a timestamped name, never overwritten.
if [ -f db/trading.db ]; then
  SAFETY="db/trading.db.pre-restore-$(date -u +%Y%m%d-%H%M%S)"
  cp -a db/trading.db "$SAFETY"
  ok "safety copy: $SAFETY"
fi

info "extracting..."
tar -xzf "$ARCHIVE"

ok "restore complete"
info "  the previous database is still at the .pre-restore-* file if needed"
info "  start the stack with: ./scripts/start.sh"
info "  then verify:         ./scripts/status.sh"

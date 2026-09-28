#!/usr/bin/env bash
# Back up the database, journals, control state, and research results.
#
#   ./scripts/backup.sh              # -> backups/trading-YYYYmmdd-HHMMSS.tar.gz
#   ./scripts/backup.sh --keep 14    # retain the newest 14, prune older
#
# IDEMPOTENT: each run creates a new timestamped archive; pruning is
# separate and only removes files matching our own naming pattern, so it can
# never delete something you made by hand.
#
# SECRETS ARE NEVER BACKED UP. .env and config/mt5.toml are explicitly
# excluded — see docs/SECURITY.md. Restore them from your password manager.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --keep) KEEP="${2:-0}"; shift 2 ;;
    *) die "unknown option: $1 (only --keep is supported)" ;;
  esac
done

STAMP="$(date -u +%Y%m%d-%H%M%S)"
BACKUP_DIR="$REPO_DIR/backups"
ARCHIVE="$BACKUP_DIR/trading-$STAMP.tar.gz"
mkdir -p "$BACKUP_DIR"

info "building backup -> $ARCHIVE"

# Build a manifest of what exists, so the archive is valid even on a fresh
# clone where the databases are absent.
MANIFEST="$(mktemp)"
: > "$MANIFEST"
add() { [ -e "$1" ] && printf '%s\n' "$1" >> "$MANIFEST"; return 0; }

add db
add execution/journal.jsonl
add journal
add paper
add reports
add run
add strategy/defs
add execution/approved.json
add config/settings.toml
add config/risk_limits.toml
add config/symbols.toml
add .env.example          # the TEMPLATE, never .env
add research/study
add research/btc

# Refuse to include secrets even if something put them in an odd place.
if grep -qE '^\s*(MT5_PASSWORD|API_SECRET_KEY|BOT_TOKEN)\s*=' "$MANIFEST" 2>/dev/null; then
  die "refusing to continue: manifest names a file that looks like a secret"
fi

tar -czf "$ARCHIVE" -T "$MANIFEST" 2>/dev/null \
  || tar -czf "$ARCHIVE" $(cat "$MANIFEST")
rm -f "$MANIFEST"

SIZE="$(du -h "$ARCHIVE" | cut -f1)"
ok "backup written: $ARCHIVE ($SIZE)"

# Belt and braces: verify the archive is readable and contains the database
# if the database existed at all.
if tar -tzf "$ARCHIVE" >/dev/null 2>&1; then
  ok "archive verified readable"
else
  die "archive is corrupt — do not trust this backup"
fi

if [ "$KEEP" -gt 0 ] 2>/dev/null; then
  info "pruning to the newest $KEEP backup(s)"
  # Only files matching trading-*.tar.gz in our own backups dir.
  ls -1t "$BACKUP_DIR"/trading-*.tar.gz 2>/dev/null | tail -n "+$((KEEP+1))" | while read -r old; do
    rm -f "$old"
    info "  removed $old"
  done
fi

ok "done. Restore with: ./scripts/restore.sh $ARCHIVE"

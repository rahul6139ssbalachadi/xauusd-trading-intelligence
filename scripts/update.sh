#!/usr/bin/env bash
# Pull the latest code and restart. The normal update path.
#
#   ./scripts/update.sh
#
# Takes a backup FIRST, because a git pull can legitimately conflict with
# local changes and a bad update should not be the thing that loses your data.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

if [ -x scripts/backup.sh ]; then
  info "taking a pre-update backup"
  ./scripts/backup.sh --keep 14
fi

info "fetching updates"
git fetch --all --tags

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
info "branch: $BRANCH"

if ! git diff --quiet 2>/dev/null; then
  warn "you have uncommitted local changes:"
  git status --short
  printf 'Continue anyway? (y/N) '
  read -r go
  [ "$go" = "y" ] || die "aborted"
fi

info "pulling"
git pull --ff-only origin "$BRANCH" \
  || die "pull failed (not a fast-forward, or conflicts). Resolve manually:
       git status
       git rebase --continue   # or git merge --abort"

info "rebuilding and restarting"
./scripts/restart.sh --rebuild

ok "update complete. Verify with: ./scripts/status.sh"

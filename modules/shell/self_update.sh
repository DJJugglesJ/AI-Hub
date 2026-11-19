#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$SCRIPT_DIR/logging.sh"

# Explicit headless requests are noninteractive; lack of a display alone is not consent.
AUTO_CONFIRM=false
HEADLESS_MODE=false
if [[ "${1:-}" == "--headless" || "${HEADLESS_UPDATE:-0}" == "1" || "${HEADLESS:-0}" == "1" ]]; then
  AUTO_CONFIRM=true
  HEADLESS_MODE=true
fi
if ! command -v yad >/dev/null 2>&1 || [[ -z "${DISPLAY:-}" ]]; then
  HEADLESS_MODE=true
fi

fail() {
  log_error "$1"
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

# rev-parse supports worktrees, where .git is a file rather than a directory.
git -C "$INSTALL_DIR" rev-parse --show-toplevel >/dev/null 2>&1 || fail "Self-update requires a Git checkout."
[[ -z "$(git -C "$INSTALL_DIR" status --porcelain)" ]] || fail "Working tree has local changes; save them before updating."
BRANCH=$(git -C "$INSTALL_DIR" symbolic-ref --quiet --short HEAD) || fail "Cannot update a detached checkout."
REMOTE=$(git -C "$INSTALL_DIR" config --get "branch.$BRANCH.remote") || fail "Current branch has no upstream remote."
REMOTE_REF=$(git -C "$INSTALL_DIR" config --get "branch.$BRANCH.merge") || fail "Current branch has no upstream branch."

if ! $AUTO_CONFIRM; then
  if $HEADLESS_MODE; then
    [[ -t 0 ]] || fail "Noninteractive updates require --headless or HEADLESS=1."
    read -r -p "Update AI Hub from its upstream branch? [y/N]: " response || exit 0
    [[ "$response" =~ ^[Yy]$ ]] || exit 0
  else
    yad --question --title="Update AI Hub" --text="Update from the current branch's upstream? A backup will be created." || exit 0
  fi
fi

LOCAL_HEAD=$(git -C "$INSTALL_DIR" rev-parse HEAD)
git -C "$INSTALL_DIR" fetch "$REMOTE" "$REMOTE_REF" >> "$LOG_FILE" 2>&1 || fail "Unable to fetch upstream; checkout unchanged."
REMOTE_HEAD=$(git -C "$INSTALL_DIR" rev-parse FETCH_HEAD)
git -C "$INSTALL_DIR" merge-base --is-ancestor "$LOCAL_HEAD" "$REMOTE_HEAD" || fail "Update is not a fast-forward; checkout unchanged."
if [[ "$LOCAL_HEAD" == "$REMOTE_HEAD" ]]; then
  log_msg "AI Hub is already up to date."
  exit 0
fi

BACKUP_DIR="${AIHUB_BACKUP_DIR:-$CONFIG_ROOT/backups}"
mkdir -p "$BACKUP_DIR"
BACKUP_FILE="$BACKUP_DIR/aihub-$(date +%Y%m%d-%H%M%S)-${LOCAL_HEAD:0:8}.tar.gz"
git -C "$INSTALL_DIR" archive --format=tar.gz -o "$BACKUP_FILE" "$LOCAL_HEAD" || fail "Unable to create backup; checkout unchanged."
# Fast-forward only: no reset --hard, no extraction over local files, no merge commit.
git -C "$INSTALL_DIR" merge --ff-only "$REMOTE_HEAD" >> "$LOG_FILE" 2>&1 || fail "Update failed; backup available at $BACKUP_FILE."
log_msg "Updated AI Hub to $REMOTE_HEAD; backup: $BACKUP_FILE"
printf 'AI Hub updated. Restart the launcher to use the new version.\n'

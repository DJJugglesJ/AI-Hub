#!/bin/bash
set -euo pipefail

# Start the AI Hub web launcher (Linux/macOS/WSL)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOST="${AIHUB_WEB_HOST:-127.0.0.1}"
PORT="${AIHUB_WEB_PORT:-3939}"

PYTHON="${AIHUB_PYTHON:-$PROJECT_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="${AIHUB_PYTHON:-python3}"
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "AI Hub requires Python 3.11 or newer (3.11 and 3.12 tested)." >&2
  exit 1
fi

cd "$PROJECT_ROOT" || exit 1

PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}" \
AIHUB_WEB_HOST="$HOST" \
AIHUB_WEB_PORT="$PORT" \
"$PYTHON" -m modules.runtime.web_launcher serve --host "$HOST" --port "$PORT"

#!/usr/bin/env bash
# Restart ONLY the backend on a given sqlite DB (frontend stays up and proxies
# /api -> :8000). Used to swap buggy/fixed code between before/after legs.
#   tools/agent/restart_backend.sh <db-file>
set -euo pipefail

# DevBox (BOW_SANDBOX_DEVBOX=1): the stack is booted, migrated and provisioned
# by the DevBox runtime and owned by `bow-stack`. Starting or killing a second
# backend here would fight it for :8000 and point at another database, so
# refuse and name the equivalent instead. See AGENTS.md, "DevBox".
if [ "${BOW_SANDBOX_DEVBOX:-0}" = "1" ]; then
  echo "restart_backend.sh: this is a DevBox (BOW_SANDBOX_DEVBOX=1); the stack is managed by bow-stack." >&2
  echo "  use: bow-stack restart backend   (or bow-stack reset-db for a fresh database)" >&2
  exit 1
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_DIR="${BOW_AGENT_RUN_DIR:-/tmp/bow-agent}"
DB_FILE="${1:?usage: restart_backend.sh <db-file>}"
DB_URL="sqlite:///$DB_FILE"
mkdir -p "$RUN_DIR"

# Stop existing backend.
if [ -f "$RUN_DIR/backend.pid" ]; then
  pid=$(cat "$RUN_DIR/backend.pid")
  kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
  sleep 2
fi

cd "$ROOT/backend"
export TESTING="true"
export ENVIRONMENT="production"
export TEST_DATABASE_URL="$DB_URL"
mkdir -p db
echo "migrating $DB_URL ..."
uv run alembic upgrade head >/dev/null 2>&1
setsid uv run python main.py > "$RUN_DIR/backend.log" 2>&1 &
echo $! > "$RUN_DIR/backend.pid"

echo "waiting for backend ..."
timeout 90 bash -c 'until curl -sf http://localhost:8000/health >/dev/null 2>&1; do sleep 1; done' \
  && echo "backend up on $DB_URL" || { echo "backend failed"; tail -20 "$RUN_DIR/backend.log"; exit 1; }

#!/usr/bin/env bash
# Reset the backend to a clean database and restart it, keeping the frontend as
# is. Used before the MCP context-forwarding Playwright E2E so a fresh signup
# always claims the single-tenant bootstrap admin.
#
#   tools/agent/reset_backend.sh
#
# Env overrides mirror boot_stack.sh:
#   BOW_AGENT_RUN_DIR   pid/log dir            (default /tmp/bow-agent)
#   TEST_DATABASE_URL   backend database URL   (default sqlite:///db/agent.db)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_DIR="${BOW_AGENT_RUN_DIR:-/tmp/bow-agent}"
DB_URL="${TEST_DATABASE_URL:-sqlite:///db/agent.db}"
mkdir -p "$RUN_DIR"

# Stop the running backend (leave the frontend up).
if [ -f "$RUN_DIR/backend.pid" ]; then
  pid=$(cat "$RUN_DIR/backend.pid")
  kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
  rm -f "$RUN_DIR/backend.pid"
fi
# Belt-and-braces: kill any stray backend on :8000.
pkill -f "python main.py" 2>/dev/null || true

cd "$ROOT/backend"
# Fresh SQLite file (only supported for the sqlite dev DB).
rm -f db/agent.db db/agent.db-shm db/agent.db-wal 2>/dev/null || true
mkdir -p db uploads/files uploads/branding

export TESTING="true"
export ENVIRONMENT="production"
export TEST_DATABASE_URL="$DB_URL"

uv run alembic upgrade head
setsid uv run python main.py > "$RUN_DIR/backend.log" 2>&1 &
echo $! > "$RUN_DIR/backend.pid"

echo "waiting for backend on :8000 ..."
curl --retry 90 --retry-delay 1 --retry-connrefused -sf -o /dev/null http://localhost:8000/health
echo "backend reset and ready"

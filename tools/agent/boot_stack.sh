#!/usr/bin/env bash
# Boot the full bagofwords stack (backend :8000 + frontend :3000) for agent
# QA / verification sessions. Mirrors the playwright-tests job in
# .github/workflows/e2e-tests.yml so local agent runs match CI behavior.
#
# Usage:
#   tools/agent/boot_stack.sh            # production build (like CI, default)
#   tools/agent/boot_stack.sh --dev      # yarn dev frontend (faster iteration)
#   tools/agent/boot_stack.sh --stop     # stop everything started by this script
#   tools/agent/boot_stack.sh --status   # show what's running
#
# Env overrides:
#   BOW_AGENT_RUN_DIR   pid/log dir            (default /tmp/bow-agent)
#   TEST_DATABASE_URL   backend database URL   (default sqlite:///db/agent.db)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_DIR="${BOW_AGENT_RUN_DIR:-/tmp/bow-agent}"
DB_URL="${TEST_DATABASE_URL:-sqlite:///db/agent.db}"
MODE="prod"

mkdir -p "$RUN_DIR"

stop_all() {
  for name in backend frontend; do
    local_pid_file="$RUN_DIR/$name.pid"
    if [ -f "$local_pid_file" ]; then
      pid=$(cat "$local_pid_file")
      if kill -0 "$pid" 2>/dev/null; then
        # Kill the whole process group so uvicorn/node children die too.
        kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
        echo "stopped $name (pid $pid)"
      fi
      rm -f "$local_pid_file"
    fi
  done
}

status_all() {
  for name in backend frontend; do
    if [ -f "$RUN_DIR/$name.pid" ] && kill -0 "$(cat "$RUN_DIR/$name.pid")" 2>/dev/null; then
      echo "$name: running (pid $(cat "$RUN_DIR/$name.pid"), log $RUN_DIR/$name.log)"
    else
      echo "$name: not running"
    fi
  done
}

wait_for() { # url, label, timeout_s
  local url="$1" label="$2" timeout="${3:-90}"
  echo "waiting for $label at $url (up to ${timeout}s)..."
  timeout "$timeout" bash -c "until curl -sf \"$url\" > /dev/null 2>&1; do sleep 1; done" \
    || { echo "ERROR: $label did not come up. Last log lines:"; tail -30 "$RUN_DIR"/*.log; exit 1; }
  echo "$label is ready"
}

case "${1:-}" in
  --stop)   stop_all; exit 0 ;;
  --status) status_all; exit 0 ;;
  --dev)    MODE="dev" ;;
  "")       ;;
  *) echo "unknown flag: $1"; exit 2 ;;
esac

# --- vendor libs ---------------------------------------------------------------
# Artifacts (dashboards/pages) embed these server-side at creation time; without
# them create_artifact/edit_artifact fail and any artifact created meanwhile is
# permanently broken. Docker builds run this script; agent sandboxes must too.
# NOTE: the script's default output path is CWD-relative, so run from $ROOT.
if [ ! -f "$ROOT/frontend/public/libs/react-18.production.min.js" ]; then
  (cd "$ROOT" && bash scripts/download-vendor-libs.sh) || echo "WARN: vendor libs download failed; artifact dashboards will not render"
fi
# The built frontend serves from .output/public — keep it in sync when it exists.
if [ -d "$ROOT/frontend/.output/public/libs" ]; then
  cp -n "$ROOT"/frontend/public/libs/*.js "$ROOT/frontend/.output/public/libs/" 2>/dev/null || true
fi

# --- backend -----------------------------------------------------------------
cd "$ROOT/backend"
command -v uv >/dev/null || pip install uv
uv sync --frozen --extra dev
mkdir -p db uploads/files uploads/branding

export TESTING="true"
export ENVIRONMENT="production"
export TEST_DATABASE_URL="$DB_URL"

echo "running migrations against $DB_URL ..."
uv run alembic upgrade head

if [ -f "$RUN_DIR/backend.pid" ] && kill -0 "$(cat "$RUN_DIR/backend.pid")" 2>/dev/null; then
  echo "backend already running (pid $(cat "$RUN_DIR/backend.pid"))"
else
  setsid uv run python main.py > "$RUN_DIR/backend.log" 2>&1 &
  echo $! > "$RUN_DIR/backend.pid"
fi
wait_for "http://localhost:8000/health" "backend" 90

# --- frontend ----------------------------------------------------------------
cd "$ROOT/frontend"
yarn install --frozen-lockfile

if [ -f "$RUN_DIR/frontend.pid" ] && kill -0 "$(cat "$RUN_DIR/frontend.pid")" 2>/dev/null; then
  echo "frontend already running (pid $(cat "$RUN_DIR/frontend.pid"))"
elif [ "$MODE" = "dev" ]; then
  setsid yarn dev > "$RUN_DIR/frontend.log" 2>&1 &
  echo $! > "$RUN_DIR/frontend.pid"
else
  # Production build, same as CI: pre-compiled routes hydrate fast and the
  # @nuxt-alt/proxy still forwards /api -> :8000 in the output server.
  NODE_OPTIONS="--max-old-space-size=4096" yarn build
  setsid node .output/server/index.mjs > "$RUN_DIR/frontend.log" 2>&1 &
  echo $! > "$RUN_DIR/frontend.pid"
fi
wait_for "http://localhost:3000" "frontend" 180

echo
echo "stack is up:  backend http://localhost:8000  frontend http://localhost:3000"
echo "logs: $RUN_DIR/{backend,frontend}.log   stop with: tools/agent/boot_stack.sh --stop"

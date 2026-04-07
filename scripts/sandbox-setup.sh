#!/usr/bin/env bash
# Sandbox setup for running the Playwright e2e suite locally.
#
# Captures the steps that worked in a fresh Claude Code sandbox where
# outbound downloads to playwright.azureedge.net and the Python wheels
# for psycopg2 (source build) are blocked, but Python 3.12, Node 22,
# yarn, and a pre-staged Playwright browser bundle in /opt/pw-browsers
# are available.
#
# Usage:
#   ./scripts/sandbox-setup.sh           # install deps + start servers
#   ./scripts/sandbox-setup.sh test      # also run the playwright suite
#
# Env exported for subsequent commands in the same shell:
#   PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
#   VENV=/tmp/bow-venv

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${VENV:-/tmp/bow-venv}"
PW_BROWSERS="${PLAYWRIGHT_BROWSERS_PATH:-/opt/pw-browsers}"

echo "==> Repo: $REPO_ROOT"
cd "$REPO_ROOT"

# ── 1. Backend Python venv (3.12 required; 3.11 fails on planner f-string) ──
if [ ! -x "$VENV/bin/python" ]; then
  echo "==> Creating venv at $VENV (python3.12)"
  python3.12 -m venv "$VENV"
fi

# psycopg2 (source) needs libpq-dev which the sandbox doesn't ship; the repo
# also pins psycopg2-binary which is sufficient for SQLite-based playwright.
echo "==> Installing backend requirements (skipping psycopg2 source build)"
grep -v '^psycopg2==' backend/requirements_versioned.txt > /tmp/bow-req.txt
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r /tmp/bow-req.txt

# ── 2. Pre-staged Playwright browsers ───────────────────────────────────────
# The sandbox blocks chromium downloads. /opt/pw-browsers ships chromium-1194
# and chromium_headless_shell-1194; symlink the build numbers Playwright 1.55
# (npm) and 1.56 (pip) expect so launch() finds the binaries.
if [ -d "$PW_BROWSERS" ]; then
  echo "==> Symlinking pre-staged Playwright browsers in $PW_BROWSERS"
  for build in 1193 1200; do
    [ -e "$PW_BROWSERS/chromium-$build" ] || \
      ln -s "$PW_BROWSERS/chromium-1194" "$PW_BROWSERS/chromium-$build"
    [ -e "$PW_BROWSERS/chromium_headless_shell-$build" ] || \
      ln -s "$PW_BROWSERS/chromium_headless_shell-1194" \
            "$PW_BROWSERS/chromium_headless_shell-$build"
  done
else
  echo "!! $PW_BROWSERS not found; you'll need to provide chromium yourself" >&2
fi
export PLAYWRIGHT_BROWSERS_PATH="$PW_BROWSERS"

# ── 3. Frontend deps ────────────────────────────────────────────────────────
echo "==> yarn install (frontend)"
( cd frontend && yarn install --frozen-lockfile )

# ── 4. Reset DB + run migrations ────────────────────────────────────────────
echo "==> Resetting playwright SQLite db and running migrations"
mkdir -p backend/db backend/uploads/files backend/uploads/branding
rm -f backend/db/playwright.db
( cd backend && \
  TESTING=true ENVIRONMENT=production \
  TEST_DATABASE_URL="sqlite:///db/playwright.db" \
  "$VENV/bin/alembic" upgrade head >/dev/null )

# ── 5. Start backend (uvicorn directly — main.py uses reload=True which
#       loops endlessly when watchfiles sees the sqlite file mutate). ───────
echo "==> Killing any prior backend/frontend processes"
pkill -9 -f "uvicorn.*main:app" 2>/dev/null || true
pkill -f "nuxt dev" 2>/dev/null || true
sleep 1

echo "==> Starting backend on :8000"
( cd backend && \
  TESTING=true ENVIRONMENT=production \
  TEST_DATABASE_URL="sqlite:///db/playwright.db" \
  nohup "$VENV/bin/uvicorn" main:app --host 0.0.0.0 --port 8000 \
  > /tmp/bow-backend.log 2>&1 & )
for i in $(seq 1 60); do
  curl -sf http://localhost:8000/api/settings >/dev/null && { echo "   backend ready ($i s)"; break; }
  sleep 1
done

# ── 6. Start frontend dev server ────────────────────────────────────────────
echo "==> Starting frontend (yarn dev) on :3000"
( cd frontend && nohup yarn dev > /tmp/bow-frontend.log 2>&1 & )
for i in $(seq 1 90); do
  curl -sf http://localhost:3000 >/dev/null 2>&1 && { echo "   frontend ready ($i s)"; break; }
  sleep 1
done

echo
echo "Backend  log: /tmp/bow-backend.log"
echo "Frontend log: /tmp/bow-frontend.log"
echo "Stop with:    pkill -f 'uvicorn.*main:app'; pkill -f 'nuxt dev'"

# ── 7. Optional: run the playwright suite ───────────────────────────────────
if [ "${1:-}" = "test" ]; then
  echo "==> Running playwright suite"
  rm -rf frontend/test-results \
         frontend/tests/config/admin.json \
         frontend/tests/config/auth.json
  unset OPENAI_API_KEY_TEST  # leave unset so the live LLM test self-skips
  ( cd frontend && \
    PLAYWRIGHT_BROWSERS_PATH="$PW_BROWSERS" \
    npx playwright test --workers=2 --reporter=list )
fi

#!/usr/bin/env bash
# Loop B for report-email bursting: boot a backend on a private port with a
# scratch SQLite DB and an SMTP sink, seed owner + 3 members + an external
# address, fire every report-email path, and summarise the captured mail.
#
#   run_loop.sh <backend_dir> <work_dir> <api_port> <smtp_port>
#
# <backend_dir> is the backend to test (a checkout of the base branch for
# "before", this branch for "after"); it needs a .venv (symlink is fine) and
# the vendored JS libs under ../frontend/public/libs for real Chromium PDFs.
# Prints the mail summary to <work_dir>/mail-summary.txt.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$(cd "$1" && pwd)"; WORK="$2"; PORT="$3"; SMTP="$4"
rm -rf "$WORK/mail" "$WORK/bursting.db"
mkdir -p "$WORK/mail"
WORK="$(cd "$WORK" && pwd)"
PY="$BACKEND/.venv/bin/python"

export ENVIRONMENT=production TESTING=true
export TEST_DATABASE_URL="sqlite:///$WORK/bursting.db"
export BOW_DATABASE_URL="$TEST_DATABASE_URL"
export BOW_CONFIG_PATH="$WORK/bow-config.yaml"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/opt/pw-browsers}"
export BOW_SANDBOX_PORT="$PORT"

"$PY" "$HERE/make_config.py" "$BACKEND/../bow-config.yaml" "$BOW_CONFIG_PATH" "$SMTP"

"$PY" "$HERE/smtp_sink.py" "$SMTP" "$WORK/mail" > "$WORK/smtp.log" 2>&1 &
SINK=$!
cleanup() { kill "$SINK" "${API:-}" 2>/dev/null || true; }
trap cleanup EXIT

cd "$BACKEND"
mkdir -p uploads/files uploads/branding
"$PY" -m alembic upgrade head > "$WORK/alembic.log" 2>&1
"$PY" "$HERE/../public-output-parity/launch.py" > "$WORK/backend.log" 2>&1 &
API=$!
for _ in $(seq 1 120); do curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break; sleep 1; done

"$PY" "$HERE/../../../tools/agent/seed_org.py" --base-url "http://127.0.0.1:$PORT" \
  --db-path "$WORK/bursting.db" \
  --invite alice@example.com --invite bob@example.com --invite carol@example.com > "$WORK/org.json"
"$PY" "$HERE/seed_bursting.py" "$WORK/ids.json" > "$WORK/seed.log" 2>&1
"$PY" "$HERE/trigger.py" "$WORK/ids.json" "http://127.0.0.1:$PORT" > "$WORK/trigger.log" 2>&1
grep -E "^(identity|plain|deck) " "$WORK/trigger.log"
"$PY" "$HERE/read_mail.py" "$WORK/mail" | tee "$WORK/mail-summary.txt"

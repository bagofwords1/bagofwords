#!/usr/bin/env bash
# Start a backend for this loop on a non-default port with its own SQLite DB.
# Usage (from backend/): ../docs/feedback-loops/identity-bind-structural/launch.sh <port> <db_file>
set -euo pipefail
export TESTING=true ENVIRONMENT=production TEST_DATABASE_URL="sqlite:///$2"
.venv/bin/python -m alembic upgrade head >/dev/null
exec .venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port "$1"

#!/usr/bin/env bash
# Restore the pristine seeded DB and restart the backend against it, so a
# before-run and an after-run start from byte-identical state.
#
# Killing the backend is fiddly and getting it wrong silently corrupts a
# benchmark: `main.py` spawns uvicorn workers through multiprocessing, and those
# children's command lines contain NEITHER "main.py" NOR "uv". A pattern kill on
# "main.py" therefore leaves the workers alive, still bound to :8000 and still
# holding an open handle on the database file this script is about to delete.
# They then keep serving happily from the deleted inode: the API answers 200
# with data that is not in db/app.db, writes vanish, and freshly created rows
# come back "not found". Kill on the venv interpreter path, which every
# descendant shares, and verify the port is actually free before restarting.
set -u
cd "$(dirname "$0")/.."
LOG=${BOW_LOG:-/tmp/backend.log}
VENV_PY="$(pwd)/.venv/bin/python3"

pkill -9 -f "$VENV_PY" 2>/dev/null
pkill -9 -f "uv run python main.py" 2>/dev/null
for i in $(seq 1 60); do
  pgrep -f "$VENV_PY" >/dev/null || break
  sleep 0.5
done
# The port must be free, or an orphan is still serving.
for i in $(seq 1 60); do
  curl -s --noproxy '*' -o /dev/null --max-time 2 http://localhost:8000/api/organizations || break
  sleep 0.5
done
if curl -s --noproxy '*' -o /dev/null --max-time 2 http://localhost:8000/api/organizations; then
  echo "FATAL: something is still serving :8000 after the kill" >&2
  ps -eo pid,cmd | grep -E "[p]ython|[u]vicorn" >&2
  exit 1
fi
sleep 1

rm -f db/app.db db/app.db-shm db/app.db-wal
cp db/pristine.db db/app.db

export BOW_DATABASE_URL="sqlite:///db/app.db" BOW_SMTP_PASSWORD=dummy ANTHROPIC_API_KEY=dummy
setsid nohup uv run python main.py > "$LOG" 2>&1 < /dev/null &
disown

for i in $(seq 1 180); do
  curl -s --noproxy '*' -o /dev/null --max-time 5 http://localhost:8000/api/organizations 2>/dev/null && { echo "backend ready"; exit 0; }
  sleep 1
done
echo "backend did not come up; see $LOG" >&2
exit 1

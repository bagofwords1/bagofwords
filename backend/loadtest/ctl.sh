#!/usr/bin/env bash
# Sandbox process control. Uses pidfiles rather than pkill/pgrep patterns:
# a pattern like "uvicorn main:app" also matches the shell that is running the
# kill command, which silently kills the caller.
cd /home/user/bagofwords/backend
source .sandbox_env
LOGS=loadtest/logs; mkdir -p "$LOGS"
BACK_PID=$LOGS/backend.pid
MOCK_PID=$LOGS/mock_llm.pid

WORKERS="${BOW_WORKERS:-2}"

_stop() { # $1=pidfile  $2=port
  # `uv run` execs the real server as a child, so $! is a wrapper pid whose
  # death leaves the server holding the port. Kill the process group first,
  # then anything still bound to the port — otherwise the next start fails
  # with EADDRINUSE and silently keeps serving the OLD code.
  if [ -f "$1" ]; then
    local pid; pid=$(cat "$1")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
    fi
    rm -f "$1"
  fi
  if [ -n "${2:-}" ]; then
    for _ in $(seq 1 20); do
      local holders; holders=$(fuser -n tcp "$2" 2>/dev/null | tr -s ' ')
      [ -z "$holders" ] && break
      # shellcheck disable=SC2086
      kill -TERM $holders 2>/dev/null || true
      sleep 0.5
    done
    fuser -k -n tcp "$2" 2>/dev/null || true
    sleep 0.5
  fi
}

start_mock() {
  _stop "$MOCK_PID" 9001
  setsid nohup uv run python loadtest/mock_llm.py > "$LOGS/mock_llm.log" 2>&1 < /dev/null &
  echo $! > "$MOCK_PID"
  until curl -sf --noproxy '*' http://127.0.0.1:9001/stats >/dev/null 2>&1; do sleep 1; done
  echo "mock_llm up (pid $(cat $MOCK_PID))"
}

start_backend() {
  _stop "$BACK_PID" 8000
  : > "$LOGS/backend.log"
  setsid nohup uv run uvicorn main:app --host 0.0.0.0 --port 8000 \
      --workers "$WORKERS" --no-access-log > "$LOGS/backend.log" 2>&1 < /dev/null &
  echo $! > "$BACK_PID"
  until curl -sf --noproxy '*' http://127.0.0.1:8000/health >/dev/null 2>&1 \
     || grep -q "startup failed" "$LOGS/backend.log"; do sleep 1; done
  if curl -sf --noproxy '*' http://127.0.0.1:8000/health >/dev/null 2>&1; then
    echo "backend up, workers=$WORKERS (pid $(cat $BACK_PID))"
  else
    echo "BACKEND FAILED TO START"; tail -20 "$LOGS/backend.log"; return 1
  fi
}

reset_db() {
  psql "postgresql://bow:bow@127.0.0.1:5432/bow" -q \
    -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" >/dev/null
  # The extension lives in the schema we just dropped; without this every
  # later query snapshot is silently empty.
  psql "postgresql://bow:bow@127.0.0.1:5432/bow" -q \
    -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;" >/dev/null 2>&1
  uv run alembic upgrade head >/dev/null 2>&1
  echo "db reset + migrated"
}

case "${1:-}" in
  start)    start_mock; start_backend ;;
  stop)     _stop "$BACK_PID" 8000; _stop "$MOCK_PID" 9001; echo stopped ;;
  restart)  _stop "$BACK_PID" 8000; start_backend ;;
  reset)    _stop "$BACK_PID" 8000; reset_db; start_backend ;;
  status)
    curl -s --noproxy '*' -o /dev/null -w "backend=%{http_code} " http://127.0.0.1:8000/health
    curl -s --noproxy '*' -o /dev/null -w "mock=%{http_code}\n" http://127.0.0.1:9001/stats ;;
  *) echo "usage: ctl.sh {start|stop|restart|reset|status}"; exit 1 ;;
esac

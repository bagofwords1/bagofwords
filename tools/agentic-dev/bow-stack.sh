#!/usr/bin/env bash
# bow-stack - start / stop / restart the Bag of Words dev stack inside the
# sandbox runtime. Safe for agents to call any number of times; the container
# keeps running regardless of what happens to these processes.
#
# Usage:
#   bow-stack start   [backend|frontend|all]      (default all)
#   bow-stack stop    [backend|frontend|all]
#   bow-stack restart [backend|frontend|all]
#   bow-stack status
#   bow-stack logs    [backend|frontend]          (tail -f)
#
# Reads its environment from ${BOW_WORKSPACE}/run/stack.env, which
# entrypoint.sh writes at boot (paths, database URL, encryption key, mode).
# Note: the backend runs uvicorn with --reload and the frontend runs nuxt dev
# with HMR, so most code changes need no restart at all.
set -euo pipefail

BOW_WORKSPACE="${BOW_WORKSPACE:-/app/workspace}"
RUN="${BOW_WORKSPACE}/run"
ENV_FILE="${RUN}/stack.env"

[[ -f "$ENV_FILE" ]] || { echo "bow-stack: ${ENV_FILE} not found; has entrypoint.sh run?" >&2; exit 1; }
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

log() { printf '[bow-stack] %s\n' "$*"; }

pid_of() { # name -> pid or empty
  local f="${RUN}/$1.pid" pid
  [[ -f "$f" ]] || return 0
  pid="$(cat "$f")"
  if kill -0 "$pid" 2>/dev/null; then echo "$pid"; else rm -f "$f"; fi
}

wait_for() { # url, label, timeout_s
  local url="$1" label="$2" timeout="${3:-120}" i
  for ((i=0; i<timeout; i++)); do
    curl -sf "$url" >/dev/null 2>&1 && { log "${label} is ready"; return 0; }
    sleep 1
  done
  log "ERROR: ${label} did not come up within ${timeout}s. Last log lines:"
  tail -n 40 "${RUN}/${label}.log" >&2 || true
  return 1
}

launch() { # name, command...   (new session so stop can kill the whole tree)
  local name="$1"; shift
  local logfile="${RUN}/${name}.log"
  : > "$logfile"
  setsid "$@" >>"$logfile" 2>&1 < /dev/null &
  echo $! > "${RUN}/${name}.pid"
  log "started ${name} (pid $!, log ${logfile})"
}

start_backend() {
  if pid="$(pid_of backend)" && [[ -n "$pid" ]]; then log "backend already running (pid ${pid})"; return 0; fi
  cd "$BOW_BACKEND_DIR"
  mkdir -p db uploads/files uploads/branding
  log "running migrations"
  alembic upgrade head
  launch backend python main.py
  wait_for "http://127.0.0.1:${BOW_BACKEND_PORT}/health" backend 180
}

start_frontend() {
  if pid="$(pid_of frontend)" && [[ -n "$pid" ]]; then log "frontend already running (pid ${pid})"; return 0; fi
  cd "$BOW_FRONTEND_DIR"
  export HOST=0.0.0.0 PORT="$BOW_FRONTEND_PORT" BOW_API_TARGET="http://127.0.0.1:${BOW_BACKEND_PORT}"
  case "$BOW_FRONTEND_MODE" in
    dev)
      launch frontend yarn dev --host 0.0.0.0 --port "$BOW_FRONTEND_PORT" ;;
    prod)
      if [[ ! -f .output/server/index.mjs || "${BOW_FRONTEND_REBUILD:-0}" == "1" ]]; then
        log "building frontend (production)"
        NODE_OPTIONS="--max-old-space-size=4096" yarn build
      fi
      launch frontend node .output/server/index.mjs ;;
    *) log "ERROR: BOW_FRONTEND_MODE must be dev or prod"; return 1 ;;
  esac
  wait_for "http://127.0.0.1:${BOW_FRONTEND_PORT}/" frontend 300
}

stop_one() { # name
  local name="$1" pid
  pid="$(pid_of "$name")"
  if [[ -z "$pid" ]]; then log "${name} not running"; return 0; fi
  # Kill the whole process group (uvicorn reloader + workers, nuxt + vite).
  kill -TERM -- "-${pid}" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  local i
  for ((i=0; i<15; i++)); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  kill -KILL -- "-${pid}" 2>/dev/null || true
  rm -f "${RUN}/${name}.pid"
  log "stopped ${name} (pid ${pid})"
}

status_all() {
  local name pid
  for name in backend frontend; do
    pid="$(pid_of "$name")"
    if [[ -n "$pid" ]]; then echo "${name}: running (pid ${pid}, log ${RUN}/${name}.log)"
    else echo "${name}: not running"; fi
  done
}

cmd="${1:-}"; target="${2:-all}"
case "$cmd" in
  start)
    [[ "$target" == backend  || "$target" == all ]] && start_backend
    [[ "$target" == frontend || "$target" == all ]] && start_frontend
    ;;
  stop)
    [[ "$target" == frontend || "$target" == all ]] && stop_one frontend
    [[ "$target" == backend  || "$target" == all ]] && stop_one backend
    ;;
  restart)
    "$0" stop "$target"
    "$0" start "$target"
    ;;
  status) status_all ;;
  logs)   exec tail -n 100 -F "${RUN}/${2:-backend}.log" ;;
  *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac

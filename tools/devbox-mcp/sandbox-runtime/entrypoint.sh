#!/usr/bin/env bash
# Entrypoint for the Bag of Words sandbox runtime image
# (tools/devbox-mcp/sandbox-runtime/Dockerfile).
#
# Brings up a fully pre-configured, ready-to-use Bag of Words dev instance:
#
#   1. sources in the persistent workspace   (git clone on first boot)
#   2. backend + frontend dependencies        (uv sync / yarn install if missing)
#   3. backend  :8000  uvicorn with reload, SQLite + uploads on the workspace
#   4. provisioning via the API: admin user + organization, an LLM provider with
#      one default model (Anthropic Claude 4.5 Haiku when ANTHROPIC_KEY is set,
#      matching .claude/skills/sandbox-feedback-loop; OpenAI only as a fallback
#      when just OPENAI_API_KEY is set), onboarding marked complete
#   5. frontend :3000  nuxt dev (default) or production build + node server
#   6. code-server :8080 and sandboxd :8888 (REST) / :9090 (gRPC) for agents
#
# Everything the instance writes lives under BOW_WORKSPACE, which the
# SandboxTemplate mounts from a per-sandbox PVC, so state survives restarts.
#
# Backend and frontend are started through `bow-stack` (bow-stack.sh), which
# agents inside the sandbox can use freely to stop / restart them:
#   bow-stack restart backend
#   bow-stack status
# The container does NOT exit when those processes stop; it only exits on
# SIGTERM from Kubernetes. That keeps the pod alive across agent-driven
# restarts. The readiness probe on the frontend simply reports not-ready
# while they are down.
#
# Combines tools/agent/boot_stack.sh and tools/agent/setup_openai_llm.py into
# one idempotent boot.
#
# Configuration (env):
#   BOW_WORKSPACE           persistent root            (default /app/workspace)
#   BOW_VENV                python venv path           (default $BOW_WORKSPACE/venv;
#                                                       /opt/venv is used if the image ships one)
#   BOW_REPO_URL            git repo to clone           (default github.com/bagofwords1/bagofwords)
#   BOW_REPO_REF            branch / tag to check out   (default main)
#   GIT_USER_NAME           fallback git identity for commits made in the devbox
#   GIT_USER_EMAIL          (defaults: "Bag of Words devbox" / devbox@bagofwords.com).
#                           Per-user credentials come from `gh auth login` run by
#                           the devbox MCP (devbox_github_login), never from here.
#   BOW_FRONTEND_MODE       dev | prod                  (default dev)
#   BOW_DATABASE_URL        SQLAlchemy URL              (default sqlite on the workspace)
#   BOW_ADMIN_EMAIL         bootstrap admin login       (default admin@example.com)
#   BOW_ADMIN_PASSWORD      bootstrap admin password    (default Password123!)
#   BOW_ADMIN_NAME          bootstrap admin name        (default Sandbox Admin)
#   BOW_LICENSE_KEY         enterprise license key; the backend reads it from the
#                           environment (bow-config `license.key` placeholder)
#   ANTHROPIC_KEY           Anthropic API key (ANTHROPIC_API_KEY also accepted); seeds an
#                           Anthropic provider with one model, default + small-default
#   ANTHROPIC_MODEL_ID      model id to register        (default claude-haiku-4-5-20251001)
#   ANTHROPIC_MODEL_NAME    display name                (default Claude 4.5 Haiku)
#   OPENAI_API_KEY          fallback when no Anthropic key: OpenAI provider + model
#   OPENAI_MODEL_ID         model id to register        (default gpt-5.6-luna)
#   OPENAI_MODEL_NAME       display name                (default GPT-5.6 Luna)
#   BOW_SKIP_PROVISION=1    skip step 4 entirely
#   BOW_ENABLE_CODE_SERVER  1|0                         (default 1)
#   BOW_ENABLE_SANDBOXD     1|0                         (default 1)
#   BOW_SYNC_DEPS=1         force uv sync / yarn install even if present
#   BOW_INSTALL_PLAYWRIGHT=1 install chromium for headless rendering (slow)
#   BOW_LINKED_DEPS=0       keep venv/node_modules on the PVC instead of the
#                           cache mount (slower boots; see deps.sh)
#   DEPS_PRUNE_AFTER        minutes before a dead devbox's venv/node_modules
#                           on the cache mount are pruned (default 1440)
#   UV_CACHE_DIR, YARN_CACHE_FOLDER, PLAYWRIGHT_BROWSERS_PATH
#                           package caches; point them at a shared volume to
#                           skip downloads on later boots (the SandboxTemplate
#                           mounts a node-local hostPath at /cache for this)
#   BOW_GIT_MIRROR          bare mirror path in that cache; cloning goes
#                           through it instead of GitHub when set
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Paths and defaults
# ---------------------------------------------------------------------------
BOW_WORKSPACE="${BOW_WORKSPACE:-/app/workspace}"
BOW_REPO_URL="${BOW_REPO_URL:-https://github.com/bagofwords1/bagofwords}"
BOW_REPO_REF="${BOW_REPO_REF:-main}"
BOW_FRONTEND_MODE="${BOW_FRONTEND_MODE:-dev}"

SRC="${BOW_WORKSPACE}/bagofwords"
BACKEND="${SRC}/backend"
FRONTEND="${SRC}/frontend"
DATA="${BOW_WORKSPACE}/data"
RUN="${BOW_WORKSPACE}/run"

BOW_ADMIN_EMAIL="${BOW_ADMIN_EMAIL:-admin@example.com}"
BOW_ADMIN_PASSWORD="${BOW_ADMIN_PASSWORD:-Password123!}"
BOW_ADMIN_NAME="${BOW_ADMIN_NAME:-Sandbox Admin}"
ANTHROPIC_KEY="${ANTHROPIC_KEY:-${ANTHROPIC_API_KEY:-}}"
ANTHROPIC_MODEL_ID="${ANTHROPIC_MODEL_ID:-claude-haiku-4-5-20251001}"
ANTHROPIC_MODEL_NAME="${ANTHROPIC_MODEL_NAME:-Claude 4.5 Haiku}"
OPENAI_MODEL_ID="${OPENAI_MODEL_ID:-gpt-5.6-luna}"
OPENAI_MODEL_NAME="${OPENAI_MODEL_NAME:-GPT-5.6 Luna}"

BACKEND_PORT=8000
FRONTEND_PORT=3000
CODE_SERVER_PORT=8080
SANDBOXD_REST_PORT=8888
SANDBOXD_GRPC_PORT=9090

# Dependency placement (venv, node_modules) is handled by deps.sh: on the
# cache mount when available (hard links, seconds), else on the PVC.
# shellcheck disable=SC1091
. /app/deps.sh
export DEBIAN_FRONTEND=noninteractive

log()  { printf '\n[entrypoint] %s\n' "$*"; }
die()  { printf '\n[entrypoint] ERROR: %s\n' "$*" >&2; exit 1; }

mkdir -p "$DATA" "$RUN"

# ---------------------------------------------------------------------------
# Auxiliary processes (code-server, sandboxd, log tailer) owned by this script
# ---------------------------------------------------------------------------
declare -A PIDS=()

start_bg() { # name, command...
  local name="$1"; shift
  local logfile="${RUN}/${name}.log"
  : > "$logfile"
  "$@" >>"$logfile" 2>&1 &
  PIDS["$name"]=$!
  log "started ${name} (pid ${PIDS[$name]}, log ${logfile})"
}

shutdown() {
  log "SIGTERM received, stopping stack"
  bow-stack stop all || true
  local name
  for name in "${!PIDS[@]}"; do
    kill "${PIDS[$name]}" 2>/dev/null || true
  done
  [[ -n "${HEARTBEAT_PID:-}" ]] && kill "$HEARTBEAT_PID" 2>/dev/null || true
  wait 2>/dev/null || true
  exit 0
}
trap shutdown TERM INT

# ---------------------------------------------------------------------------
# 1. Sources
# ---------------------------------------------------------------------------
if [[ ! -d "${SRC}/.git" ]]; then
  if [[ -n "${BOW_GIT_MIRROR:-}" ]]; then
    # Keep a bare mirror in the shared cache; clone from it, then point the
    # checkout's origin back at GitHub so pulls and pushes work normally.
    if [[ -d "$BOW_GIT_MIRROR" ]]; then
      log "updating git mirror ${BOW_GIT_MIRROR}"
      git -C "$BOW_GIT_MIRROR" fetch --prune origin || log "WARN: mirror fetch failed, using cached state"
    else
      log "creating git mirror ${BOW_GIT_MIRROR}"
      mkdir -p "$(dirname "$BOW_GIT_MIRROR")"
      git clone --mirror "$BOW_REPO_URL" "$BOW_GIT_MIRROR"
    fi
    log "cloning ${BOW_REPO_REF} from the mirror into ${SRC}"
    git clone --branch "$BOW_REPO_REF" "$BOW_GIT_MIRROR" "$SRC"
    git -C "$SRC" remote set-url origin "$BOW_REPO_URL"
  else
    log "cloning ${BOW_REPO_URL} (${BOW_REPO_REF}) into ${SRC}"
    git clone --depth 1 --branch "$BOW_REPO_REF" "$BOW_REPO_URL" "$SRC"
  fi
else
  log "sources present at ${SRC} ($(git -C "$SRC" rev-parse --short HEAD))"
fi

# ---------------------------------------------------------------------------
# 1b. Git identity fallback
# ---------------------------------------------------------------------------
# Real push credentials are per user: the devbox MCP runs `gh auth login`
# (GitHub device flow) inside the devbox and `gh auth setup-git` wires git to
# it. This only sets a placeholder identity so commits never fail on a missing
# user.name/email before that happens.
git config --global user.name  "${GIT_USER_NAME:-Bag of Words devbox}"
git config --global user.email "${GIT_USER_EMAIL:-devbox@bagofwords.com}"
git config --global --add safe.directory "$SRC"

# ---------------------------------------------------------------------------
# 2. Dependencies (hard-linked from the cache mount; see deps.sh)
# ---------------------------------------------------------------------------
if deps_cache_usable; then
  log "pruning stale dependency dirs on ${DEPS_CACHE}"
  deps_prune
  HEARTBEAT_PID="$(deps_heartbeat_start)"
fi
log "setting up backend venv"
VENV="$(deps_setup_venv "$BACKEND")"
# deps_setup_venv ran in a subshell, so re-export what it decided; otherwise the
# image's UV_PROJECT_ENVIRONMENT (/opt/venv, which does not exist) leaks into
# stack.env and every later `uv run`/`uv sync` builds a throwaway venv.
export UV_PROJECT_ENVIRONMENT="$VENV" PATH="${VENV}/bin:${PATH}"
case "$VENV" in "${DEPS_CACHE}"/*) export UV_LINK_MODE=hardlink ;; *) export UV_LINK_MODE=copy ;; esac
log "backend venv: ${VENV}"
log "setting up frontend node_modules"
NODE_MODULES="$(deps_setup_node_modules "$FRONTEND")"
log "frontend node_modules: ${NODE_MODULES}"

# Vendored JS libs are embedded into artifacts server-side; without them
# dashboards never render. Same guard as boot_stack.sh.
if [[ ! -f "${FRONTEND}/public/libs/react-18.production.min.js" ]]; then
  log "downloading vendored JS libraries"
  (cd "$SRC" && bash scripts/download-vendor-libs.sh) \
    || log "WARN: vendor libs download failed; artifact dashboards will not render"
fi

if [[ "${BOW_INSTALL_PLAYWRIGHT:-0}" == "1" && ! -d "${HOME:-/root}/.cache/ms-playwright" ]]; then
  log "installing playwright chromium"
  playwright install chromium --with-deps || log "WARN: playwright install failed"
fi

# ---------------------------------------------------------------------------
# 3. Backend
# ---------------------------------------------------------------------------
# Encryption key: persisted on the workspace so stored credentials survive
# restarts (mirrors what start.sh does for the production image).
KEY_FILE="${DATA}/encryption.key"
if [[ -z "${BOW_ENCRYPTION_KEY:-}" ]]; then
  if [[ -s "$KEY_FILE" ]]; then
    BOW_ENCRYPTION_KEY="$(tr -d '\r\n' < "$KEY_FILE")"
  else
    BOW_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
    (umask 077 && printf '%s\n' "$BOW_ENCRYPTION_KEY" > "$KEY_FILE")
    log "generated encryption key at ${KEY_FILE}"
  fi
fi
export BOW_ENCRYPTION_KEY

DB_URL="${BOW_DATABASE_URL:-sqlite:///${DATA}/bow.db}"

# Environment shared with bow-stack, so later restarts by an agent get the
# exact same settings as the initial boot. Values are %q-quoted so the file
# can be sourced even when they contain spaces.
write_env() { # file, VAR...
  local f="$1"; shift; : > "$f"
  for v in "$@"; do printf '%s=%q\n' "$v" "${!v:-}" >> "$f"; done
}
export BOW_WORKSPACE BOW_BACKEND_DIR="$BACKEND" BOW_FRONTEND_DIR="$FRONTEND" BOW_BACKEND_PORT="$BACKEND_PORT" \
       BOW_FRONTEND_PORT="$FRONTEND_PORT" BOW_FRONTEND_MODE BOW_FRONTEND_REBUILD="${BOW_FRONTEND_REBUILD:-0}" \
       TESTING=true ENVIRONMENT=production TEST_DATABASE_URL="$DB_URL" BOW_DATABASE_URL="$DB_URL" \
       BOW_ENCRYPTION_KEY FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-*}" UV_PROJECT_ENVIRONMENT \
       UV_CACHE_DIR="${UV_CACHE_DIR:-}" YARN_CACHE_FOLDER="${YARN_CACHE_FOLDER:-}" \
       PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-}" BOW_SRC_DIR="$SRC" PATH \
       UV_LINK_MODE BOW_NODE_MODULES_DIR="${NODE_MODULES}" BOW_SANDBOX_NAME="$(deps_devbox_name)"
# BOW_LICENSE_KEY (from the bow-sandbox-config Secret) must survive `bow-stack
# restart backend` from a remote-control shell, which only sees stack.env.
STACK_VARS=(BOW_WORKSPACE BOW_BACKEND_DIR BOW_FRONTEND_DIR BOW_BACKEND_PORT BOW_FRONTEND_PORT
  BOW_FRONTEND_MODE BOW_FRONTEND_REBUILD TESTING ENVIRONMENT TEST_DATABASE_URL BOW_DATABASE_URL BOW_ENCRYPTION_KEY
  FORWARDED_ALLOW_IPS UV_PROJECT_ENVIRONMENT UV_LINK_MODE UV_CACHE_DIR YARN_CACHE_FOLDER PLAYWRIGHT_BROWSERS_PATH
  BOW_SRC_DIR BOW_NODE_MODULES_DIR BOW_SANDBOX_NAME PATH)
[[ -n "${BOW_LICENSE_KEY:-}" ]] && { export BOW_LICENSE_KEY; STACK_VARS+=(BOW_LICENSE_KEY); }
(umask 077 && write_env "${RUN}/stack.env" "${STACK_VARS[@]}")

# Provisioning inputs, re-used by `bow-stack reset-db` / `bow-stack provision`
# (processes started through sandboxd do not inherit the pod environment).
export BACKEND_PORT BOW_ADMIN_EMAIL BOW_ADMIN_PASSWORD BOW_ADMIN_NAME ANTHROPIC_KEY ANTHROPIC_MODEL_ID \
       ANTHROPIC_MODEL_NAME OPENAI_API_KEY="${OPENAI_API_KEY:-}" OPENAI_MODEL_ID OPENAI_MODEL_NAME
(umask 077 && write_env "${RUN}/provision.env" BACKEND_PORT BOW_ADMIN_EMAIL BOW_ADMIN_PASSWORD BOW_ADMIN_NAME \
  ANTHROPIC_KEY ANTHROPIC_MODEL_ID ANTHROPIC_MODEL_NAME OPENAI_API_KEY OPENAI_MODEL_ID OPENAI_MODEL_NAME)

bow-stack start backend || die "backend failed to start"

# ---------------------------------------------------------------------------
# 4. Provisioning: admin + org, onboarding, LLM provider
# ---------------------------------------------------------------------------
if [[ "${BOW_SKIP_PROVISION:-0}" != "1" ]]; then
  log "provisioning admin user, organization and LLM"
  set -a; . "${RUN}/provision.env"; set +a
  # Not fatal: a failure here (LLM key rejected, password changed in the Secret,
  # ...) must not crash-loop the pod. The summary flags it and `bow-stack
  # provision` / `bow-stack reset-db` repair it from inside the devbox.
  if python /app/provision.py; then
    PROVISION_STATUS="ok"
    log "provisioning done"
  else
    PROVISION_STATUS="FAILED (see log above; run 'bow-stack provision' or 'bow-stack reset-db')"
    log "WARNING: provisioning failed; continuing so the devbox stays reachable"
  fi
else
  PROVISION_STATUS="skipped (BOW_SKIP_PROVISION=1)"
  log "BOW_SKIP_PROVISION=1, skipping provisioning"
fi

# ---------------------------------------------------------------------------
# 5. Frontend (started last: once it answers, the instance is fully usable)
# ---------------------------------------------------------------------------
bow-stack start frontend || die "frontend failed to start"

# ---------------------------------------------------------------------------
# 6. Agent access: code-server and sandboxd
# ---------------------------------------------------------------------------
if [[ "${BOW_ENABLE_CODE_SERVER:-1}" == "1" ]] && command -v code-server >/dev/null; then
  start_bg code-server code-server --bind-addr "0.0.0.0:${CODE_SERVER_PORT}" --auth none "$SRC"
fi

if [[ "${BOW_ENABLE_SANDBOXD:-1}" == "1" ]] && command -v sandboxd >/dev/null; then
  start_bg sandboxd sandboxd \
    --listen-host 0.0.0.0 \
    --rest-port "$SANDBOXD_REST_PORT" \
    --grpc-port "$SANDBOXD_GRPC_PORT" \
    --root-dir "$BOW_WORKSPACE"
fi

# ---------------------------------------------------------------------------
# Foreground: stream logs to stdout, then block until SIGTERM.
# Nothing started above is supervised. Backend / frontend restarts (agents via
# bow-stack, or crashes) and helper exits never end the container; only a
# SIGTERM from Kubernetes does, via the trap -> shutdown.
# ---------------------------------------------------------------------------
start_bg tail tail -n 0 -F "${RUN}/backend.log" "${RUN}/frontend.log"

cat <<SUMMARY

Bag of Words sandbox is up
  frontend     http://0.0.0.0:${FRONTEND_PORT}   (${BOW_FRONTEND_MODE} mode)
  backend      http://0.0.0.0:${BACKEND_PORT}
  code-server  http://0.0.0.0:${CODE_SERVER_PORT}
  sandboxd     rest :${SANDBOXD_REST_PORT}  grpc :${SANDBOXD_GRPC_PORT}
  admin        ${BOW_ADMIN_EMAIL} / ${BOW_ADMIN_PASSWORD}
  provisioning ${PROVISION_STATUS}
  license      $([[ -n "${BOW_LICENSE_KEY:-}" ]] && echo "set (BOW_LICENSE_KEY)" || echo "none (community edition)")
  sources      ${SRC}
  venv         ${VENV}
  node_modules ${NODE_MODULES}
  database     ${DB_URL}
  backend log  ${RUN}/backend.log
  frontend log ${RUN}/frontend.log
  other logs   ${RUN}/code-server.log  ${RUN}/sandboxd.log
  control      bow-stack start|stop|restart|status|logs [backend|frontend|all]

SUMMARY

# Park here. `wait` on a background sleep is interruptible by the TERM trap;
# a bare foreground `sleep infinity` would delay the trap until it returned.
sleep infinity &
wait $!

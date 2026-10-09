#!/usr/bin/env bash
# Entrypoint for the Bag of Words sandbox runtime image
# (tools/agentic-dev/Dockerfile.sandbox-runtime).
#
# Brings up a fully pre-configured, ready-to-use Bag of Words dev instance:
#
#   1. sources in the persistent workspace   (git clone on first boot)
#   2. backend + frontend dependencies        (uv sync / yarn install if missing)
#   3. backend  :8000  uvicorn with reload, SQLite + uploads on the workspace
#   4. provisioning via the API: admin user + organization, OpenAI provider and
#      default model (when OPENAI_API_KEY is set), onboarding marked complete
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
# Combines tools/agent/boot_stack.sh, tools/agent/setup_openai_llm.py and
# tools/agentic-dev/setup-runtime.sh into one idempotent boot.
#
# Configuration (env):
#   BOW_WORKSPACE           persistent root            (default /app/workspace)
#   BOW_VENV                python venv path           (default $BOW_WORKSPACE/venv;
#                                                       /opt/venv is used if the image ships one)
#   BOW_REPO_URL            git repo to clone           (default github.com/bagofwords1/bagofwords)
#   BOW_REPO_REF            branch / tag to check out   (default main)
#   BOW_FRONTEND_MODE       dev | prod                  (default dev)
#   BOW_DATABASE_URL        SQLAlchemy URL              (default sqlite on the workspace)
#   BOW_ADMIN_EMAIL         bootstrap admin login       (default admin@example.com)
#   BOW_ADMIN_PASSWORD      bootstrap admin password    (default Password123!)
#   BOW_ADMIN_NAME          bootstrap admin name        (default Sandbox Admin)
#   OPENAI_API_KEY          if set, an OpenAI provider + default model are created
#   OPENAI_MODEL_ID         model id to register        (default gpt-5.6-luna)
#   OPENAI_MODEL_NAME       display name                (default GPT-5.6 Luna)
#   BOW_SKIP_PROVISION=1    skip step 4 entirely
#   BOW_ENABLE_CODE_SERVER  1|0                         (default 1)
#   BOW_ENABLE_SANDBOXD     1|0                         (default 1)
#   BOW_SYNC_DEPS=1         force uv sync / yarn install even if present
#   BOW_INSTALL_PLAYWRIGHT=1 install chromium for headless rendering (slow)
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
OPENAI_MODEL_ID="${OPENAI_MODEL_ID:-gpt-5.6-luna}"
OPENAI_MODEL_NAME="${OPENAI_MODEL_NAME:-GPT-5.6 Luna}"

BACKEND_PORT=8000
FRONTEND_PORT=3000
CODE_SERVER_PORT=8080
SANDBOXD_REST_PORT=8888
SANDBOXD_GRPC_PORT=9090

# Python venv location. A container restart discards the image's writable
# layer, so a venv at /opt/venv (the image default) is lost and re-synced on
# every restart. Keep it on the persistent workspace instead, unless the
# image already ships a populated venv (baked at build time).
if [[ -x /opt/venv/bin/uvicorn ]]; then
  export UV_PROJECT_ENVIRONMENT=/opt/venv
else
  export UV_PROJECT_ENVIRONMENT="${BOW_VENV:-${BOW_WORKSPACE}/venv}"
fi
export PATH="${UV_PROJECT_ENVIRONMENT}/bin:${PATH}"
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
  wait 2>/dev/null || true
  exit 0
}
trap shutdown TERM INT

# ---------------------------------------------------------------------------
# 1. Sources
# ---------------------------------------------------------------------------
if [[ ! -d "${SRC}/.git" ]]; then
  log "cloning ${BOW_REPO_URL} (${BOW_REPO_REF}) into ${SRC}"
  git clone --depth 1 --branch "$BOW_REPO_REF" "$BOW_REPO_URL" "$SRC"
else
  log "sources present at ${SRC} ($(git -C "$SRC" rev-parse --short HEAD))"
fi

# ---------------------------------------------------------------------------
# 2. Dependencies (skipped when already installed, forced with BOW_SYNC_DEPS=1)
# ---------------------------------------------------------------------------
if [[ "${BOW_SYNC_DEPS:-0}" == "1" || ! -x "${UV_PROJECT_ENVIRONMENT}/bin/uvicorn" ]]; then
  log "installing backend dependencies"
  (cd "$BACKEND" && uv sync --frozen --no-install-project --extra kerberos --extra dev)
fi

if [[ "${BOW_SYNC_DEPS:-0}" == "1" || ! -d "${FRONTEND}/node_modules" ]]; then
  log "installing frontend dependencies"
  (cd "$FRONTEND" && yarn install --frozen-lockfile)
fi

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
# exact same settings as the initial boot.
cat > "${RUN}/stack.env" <<ENV
BOW_WORKSPACE=${BOW_WORKSPACE}
BOW_BACKEND_DIR=${BACKEND}
BOW_FRONTEND_DIR=${FRONTEND}
BOW_BACKEND_PORT=${BACKEND_PORT}
BOW_FRONTEND_PORT=${FRONTEND_PORT}
BOW_FRONTEND_MODE=${BOW_FRONTEND_MODE}
BOW_FRONTEND_REBUILD=${BOW_FRONTEND_REBUILD:-0}
TESTING=true
ENVIRONMENT=production
TEST_DATABASE_URL=${DB_URL}
BOW_DATABASE_URL=${DB_URL}
BOW_ENCRYPTION_KEY=${BOW_ENCRYPTION_KEY}
FORWARDED_ALLOW_IPS=${FORWARDED_ALLOW_IPS:-*}
UV_PROJECT_ENVIRONMENT=${UV_PROJECT_ENVIRONMENT}
PATH=${PATH}
ENV
chmod 600 "${RUN}/stack.env"

bow-stack start backend || die "backend failed to start"

# ---------------------------------------------------------------------------
# 4. Provisioning: admin + org, onboarding, LLM provider
# ---------------------------------------------------------------------------
if [[ "${BOW_SKIP_PROVISION:-0}" != "1" ]]; then
  log "provisioning admin user, organization and LLM"
  BOW_ADMIN_EMAIL="$BOW_ADMIN_EMAIL" BOW_ADMIN_PASSWORD="$BOW_ADMIN_PASSWORD" \
  BOW_ADMIN_NAME="$BOW_ADMIN_NAME" OPENAI_MODEL_ID="$OPENAI_MODEL_ID" \
  OPENAI_MODEL_NAME="$OPENAI_MODEL_NAME" BACKEND_PORT="$BACKEND_PORT" \
  python - <<'PY'
import os, sys, httpx

base = f"http://127.0.0.1:{os.environ['BACKEND_PORT']}"
email, password, name = (os.environ["BOW_ADMIN_EMAIL"],
                         os.environ["BOW_ADMIN_PASSWORD"],
                         os.environ["BOW_ADMIN_NAME"])
c = httpx.Client(base_url=base, timeout=60)

def ok(r, *codes):
    if r.status_code not in codes:
        sys.exit(f"{r.request.method} {r.request.url.path} -> {r.status_code} {r.text[:300]}")
    return r

# Admin user. The first registered user bootstraps an organization
# automatically (see on_after_register in backend/app/core/auth.py).
r = c.post("/api/auth/register", json={"email": email, "password": password, "name": name})
if r.status_code in (200, 201):
    print(f"[provision] created admin {email}")
elif r.status_code == 400 and "ALREADY_EXISTS" in r.text:
    print(f"[provision] admin {email} already exists")
else:
    ok(r, 201)

tok = ok(c.post("/api/auth/jwt/login", data={"username": email, "password": password}), 200).json()["access_token"]
orgs = ok(c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}), 200).json()
if not orgs:
    sys.exit("[provision] no organization after registration")
org = orgs[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}
print(f"[provision] organization {org}")

# LLM provider + default model (optional, only when a key is present).
key = os.environ.get("OPENAI_API_KEY", "").strip()
if key:
    model_id, model_name = os.environ["OPENAI_MODEL_ID"], os.environ["OPENAI_MODEL_NAME"]
    providers = ok(c.get("/api/llm/providers", headers=H), 200).json()
    prov = next((p for p in providers if p.get("provider_type") == "openai"), None)
    if prov:
        pid = prov["id"]
        ok(c.put(f"/api/llm/providers/{pid}", json={"credentials": {"api_key": key}, "is_enabled": True}, headers=H), 200)
        print(f"[provision] reusing openai provider {pid}")
    else:
        pid = ok(c.post("/api/llm/providers", json={
            "name": "OpenAI", "provider_type": "openai", "credentials": {"api_key": key}}, headers=H), 200, 201).json()["id"]
        print(f"[provision] created openai provider {pid}")
    t = c.post("/api/llm/test_connection", json={
        "name": "OpenAI", "provider_type": "openai", "provider_id": pid, "credentials": {"api_key": key}}, headers=H)
    print(f"[provision] provider test_connection: {t.status_code} {t.text[:120]}")

    models = ok(c.get("/api/llm/models", headers=H), 200).json()
    existing = next((m for m in models if m.get("model_id") == model_id), None)
    if existing:
        ok(c.patch(f"/api/llm/models/{existing['id']}",
                   json={"is_default": True, "is_small_default": True, "is_enabled": True}, headers=H), 200)
        print(f"[provision] defaulted existing model {existing['id']}")
    else:
        mid = ok(c.post("/api/llm/models", json={
            "provider_id": pid, "name": model_name, "model_id": model_id,
            "is_default": True, "is_small_default": True,
            "context_window_tokens": 1050000, "max_output_tokens": 128000}, headers=H), 200, 201).json().get("id")
        print(f"[provision] created default model {mid}")
else:
    print("[provision] OPENAI_API_KEY not set; skipping LLM provider setup")

# Mark onboarding complete so the UI opens on the workspace, not the wizard.
ok(c.put("/api/organization/onboarding", json={"completed": True, "dismissed": True}, headers=H), 200)
print("[provision] onboarding marked complete")
PY
  log "provisioning done"
else
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
  sources      ${SRC}
  database     ${DB_URL}
  logs         ${RUN}/*.log
  control      bow-stack start|stop|restart|status [backend|frontend|all]

SUMMARY

# Park here. `wait` on a background sleep is interruptible by the TERM trap;
# a bare foreground `sleep infinity` would delay the trap until it returned.
sleep infinity &
wait $!

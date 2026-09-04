#!/usr/bin/env bash
# Production entrypoint for the Bow data edge agent.
#
# Responsibilities, in order: bridge the design's documented env names to the
# ones the code reads, point the credential store and audit trail at a
# persistent data dir, fail fast on missing required configuration, then hand
# off to the agent as the process the init (tini) supervises.
#
# Secrets are never echoed. tini is PID 1 (see the Dockerfile ENTRYPOINT), so
# `exec` here makes the Python process tini's direct child and SIGTERM reaches
# the agent's own handler for a clean drain.
set -euo pipefail

log() { echo "[entrypoint] $*"; }
die() { echo "[entrypoint] ERROR: $*" >&2; exit 1; }

P=BOW_EDGE_AGENT   # env prefix the agent reads (config.py _ENV_OVERRIDES)

# ── compatibility: design doc (E1) names → implementation names ──────────────
# The design wrote BOW_EDGE_AGENT_AGENT_ID / _AGENT_NAME / _SECRET_KEY; the code
# reads EDGE_AGENT_ID / EDGE_AGENT_NAME / STORE_KEY. Bridge them so either works,
# without ever overwriting a value the operator set explicitly.
bridge() { # old_suffix new_suffix
  local old="${P}_$1" new="${P}_$2"
  if [ -n "${!old:-}" ] && [ -z "${!new:-}" ]; then
    export "$new"="${!old}"
  fi
}
bridge AGENT_ID   EDGE_AGENT_ID
bridge AGENT_NAME EDGE_AGENT_NAME
bridge SECRET_KEY STORE_KEY
# nats_url was renamed to tunnel_endpoint_url; accept the old env var too.
bridge NATS_URL   TUNNEL_ENDPOINT_URL

# ── data directory: persist the store, its key, and the audit trail ──────────
DATA_DIR="${BOW_EDGE_AGENT_DATA_DIR:-/data}"
if ! mkdir -p "$DATA_DIR" 2>/dev/null || [ ! -w "$DATA_DIR" ]; then
  die "data dir '$DATA_DIR' is not writable. Mount a volume there owned by uid 10001, or set BOW_EDGE_AGENT_DATA_DIR."
fi
# Default the store/audit locations into the data dir unless pinned elsewhere,
# so a mounted volume is all it takes to make admin-UI edits and the audit log
# survive a restart.
export BOW_EDGE_AGENT_STORE_PATH="${BOW_EDGE_AGENT_STORE_PATH:-$DATA_DIR/edge-agent-store.json}"
export BOW_EDGE_AGENT_AUDIT_PATH="${BOW_EDGE_AGENT_AUDIT_PATH:-$DATA_DIR/edge-agent-audit.jsonl}"

# ── config source: file (if given/present) OR pure environment ───────────────
CONFIG_ARGS=()
CONFIG_FILE="${BOW_EDGE_AGENT_CONFIG:-}"
if [ -z "$CONFIG_FILE" ] && [ -f /etc/bow/edge-agent.yaml ]; then
  CONFIG_FILE=/etc/bow/edge-agent.yaml
fi
if [ -n "$CONFIG_FILE" ]; then
  [ -f "$CONFIG_FILE" ] || die "config file '$CONFIG_FILE' not found"
  CONFIG_ARGS=(--config "$CONFIG_FILE")
  log "config file: $CONFIG_FILE (env overrides still apply)"
else
  # No file — identity and broker must come from the environment.
  : "${BOW_EDGE_AGENT_TUNNEL_ENDPOINT_URL:?set BOW_EDGE_AGENT_TUNNEL_ENDPOINT_URL (e.g. wss://tunnel.bow.com)}"
  : "${BOW_EDGE_AGENT_ORG_ID:?set BOW_EDGE_AGENT_ORG_ID}"
  : "${BOW_EDGE_AGENT_EDGE_AGENT_ID:?set BOW_EDGE_AGENT_EDGE_AGENT_ID (or BOW_EDGE_AGENT_AGENT_ID)}"
  log "config from environment (no config file)"
fi

# ── advisories (non-fatal) ───────────────────────────────────────────────────
if [ -z "${BOW_EDGE_AGENT_NATS_TOKEN:-}" ]; then
  log "WARNING: BOW_EDGE_AGENT_NATS_TOKEN is unset — the broker will reject the connection unless it allows anonymous access."
fi
if [ -z "${BOW_EDGE_AGENT_STORE_KEY:-}" ]; then
  log "WARNING: BOW_EDGE_AGENT_STORE_KEY is unset — a key file will be generated under $DATA_DIR. Supply the key from your own secret management, and keep $DATA_DIR on a persistent volume, or stored credentials will be lost on redeploy."
fi

log "starting edge agent: org=${BOW_EDGE_AGENT_ORG_ID:-<file>} id=${BOW_EDGE_AGENT_EDGE_AGENT_ID:-<file>} tunnel=${BOW_EDGE_AGENT_TUNNEL_ENDPOINT_URL:-<file>} admin=${BOW_EDGE_AGENT_ADMIN_HOST:-127.0.0.1}:${BOW_EDGE_AGENT_ADMIN_PORT:-9191} data_dir=$DATA_DIR"

# exec: replace the shell so the Python process is tini's direct child and owns
# the signal handling (main.py installs SIGTERM/SIGINT handlers for a clean drain).
exec python -m data_edge_agent "${CONFIG_ARGS[@]}" "$@"

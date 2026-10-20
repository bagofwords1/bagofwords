#!/usr/bin/env bash
# Boot a data edge agent against the k8s test rig (NATS + PostgreSQL in the
# bow-test namespace) for agent QA / verification sessions. Companion to
# boot_stack.sh, which boots the control plane.
#
# Runs in-cluster by default: the agent reaches the NATS and PostgreSQL
# services directly by their cluster DNS names, so no port-forwarding is
# needed. On a developer host, where those ClusterIP services are unreachable,
# set BOW_EDGE_PORT_FORWARD=1 to forward them to localhost first.
#
# Usage:
#   tools/agent/boot_data_edge_agent.sh            # start (in-cluster)
#   tools/agent/boot_data_edge_agent.sh --stop     # stop everything started here
#   tools/agent/boot_data_edge_agent.sh --status   # show what's running
#
# Env overrides:
#   BOW_AGENT_RUN_DIR         pid/log dir             (default /tmp/bow-agent)
#   BOW_EDGE_AGENT_CONFIG     agent config path       (default $RUN_DIR/edge-agent.yaml, generated)
#   NAMESPACE                 k8s namespace of the data sources (default bow-test)
#   NATS_NAMESPACE            k8s namespace of the broker       (default nats)
#   BOW_EDGE_PORT_FORWARD     1 to forward to localhost (default 0, in-cluster)
#   NATS_LOCAL_PORT           local TLS port, forward mode (default 4222)
#   PG_LOCAL_PORT             local pg port, forward mode  (default 5432)
#   BOW_EDGE_ORG_ID           org the agent advertises under (default cust-b)
#   BOW_EDGE_AGENT_ID         edge_agent_id                  (default nyc-01)
#   BOW_EDGE_CERT_DIR         mTLS material dir (default $ROOT/data_plane/certs)
#   BOW_EDGE_TLS_CA/CERT/KEY  explicit PEM paths (default $CERT_DIR/{ca,client,client-key}.pem)
#   BOW_EDGE_TLS_VERIFY       verify the broker cert (default true; forward
#                             mode needs the broker SAN to cover localhost, or false)
#   BOW_EDGE_NATS_URL         override the generated tunnel_endpoint_url
#
# mTLS is the only auth the agent supports: the cert/key must exist before this
# runs. Provision them with the Data Tunnels wizard (download the bundle and
# point BOW_EDGE_CERT_DIR at its certs/ dir) or by hand as described in
# .claude/skills/data-edge-agent-sandbox/SKILL.md.
set -euo pipefail

# Job control, so every background job below lands in its own process group and
# the group kill in stop_all reaches the children too. On Linux setsid does this
# job; macOS has no setsid, and this is what stands in for it there.
set -m

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_DIR="${BOW_AGENT_RUN_DIR:-/tmp/bow-agent}"
NAMESPACE="${NAMESPACE:-bow-test}"
NATS_NAMESPACE="${NATS_NAMESPACE:-nats}"
NATS_LOCAL_PORT="${NATS_LOCAL_PORT:-4222}"
PG_LOCAL_PORT="${PG_LOCAL_PORT:-5432}"
# In-cluster by default; the runtime pod runs inside the cluster and reaches the
# services directly. A developer host sets this to 1 to forward them first.
PORT_FORWARD="${BOW_EDGE_PORT_FORWARD:-0}"
CONFIG="${BOW_EDGE_AGENT_CONFIG:-$RUN_DIR/edge-agent.yaml}"
ORG_ID="${BOW_EDGE_ORG_ID:-cust-b}"
EDGE_AGENT_ID="${BOW_EDGE_AGENT_ID:-nyc-01}"
CERT_DIR="${BOW_EDGE_CERT_DIR:-$ROOT/data_plane/certs}"
TLS_CA="${BOW_EDGE_TLS_CA:-$CERT_DIR/ca.pem}"
TLS_CERT="${BOW_EDGE_TLS_CERT:-$CERT_DIR/client.pem}"
TLS_KEY="${BOW_EDGE_TLS_KEY:-$CERT_DIR/client-key.pem}"
TLS_VERIFY="${BOW_EDGE_TLS_VERIFY:-true}"

# Where the generated config points. In-cluster it is the services' own DNS
# names; in forward mode it is the localhost ends of the port-forwards.
if [ "$PORT_FORWARD" = "1" ]; then
  NATS_HOST="localhost"
  NATS_PORT="$NATS_LOCAL_PORT"
  PG_HOST="localhost"
  PG_PORT="$PG_LOCAL_PORT"
  MYSQL_HOST="localhost"
  MYSQL_PORT="${MYSQL_LOCAL_PORT:-13306}"
else
  NATS_HOST="nats.$NATS_NAMESPACE.svc.cluster.local"
  NATS_PORT="4222"
  PG_HOST="postgresql.$NAMESPACE.svc.cluster.local"
  PG_PORT="5432"
  MYSQL_HOST="mysql.$NAMESPACE.svc.cluster.local"
  MYSQL_PORT="3306"
fi
# Native TLS, not websocket: the client cert is exchanged in the TLS handshake,
# and a plain ws:// carries none, which the agent rejects at start-up.
NATS_URL="${BOW_EDGE_NATS_URL:-tls://$NATS_HOST:$NATS_PORT}"

# Everything this script starts, so --stop and --status stay in step with it.
# The forwards are only started in forward mode, but listing them always is
# harmless: --status just reports them "not running" in-cluster.
COMPONENTS="nats-forward pg-forward edge-agent"

mkdir -p "$RUN_DIR"

# ── Process helpers ─────────────────────────────────────────────────────────

stop_all() {
  for name in $COMPONENTS; do
    local_pid_file="$RUN_DIR/$name.pid"
    if [ -f "$local_pid_file" ]; then
      pid=$(cat "$local_pid_file")
      if kill -0 "$pid" 2>/dev/null; then
        # Kill the whole process group so `uv run`'s python child dies too.
        kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
        echo "stopped $name (pid $pid)"
      fi
      rm -f "$local_pid_file"
    fi
  done
}

status_all() {
  for name in $COMPONENTS; do
    if [ -f "$RUN_DIR/$name.pid" ] && kill -0 "$(cat "$RUN_DIR/$name.pid")" 2>/dev/null; then
      echo "$name: running (pid $(cat "$RUN_DIR/$name.pid"), log $RUN_DIR/$name.log)"
    else
      echo "$name: not running"
    fi
  done
}

running() { # name
  [ -f "$RUN_DIR/$1.pid" ] && kill -0 "$(cat "$RUN_DIR/$1.pid")" 2>/dev/null
}

start_bg() { # name, command...
  local name="$1"; shift
  if running "$name"; then
    echo "$name already running (pid $(cat "$RUN_DIR/$name.pid"))"
    return
  fi
  if command -v setsid >/dev/null 2>&1; then
    # setsid forks and exits, so $! would be the transient wrapper, not the
    # command - a dead pid that makes the liveness checks below see the process
    # as already exited. Record the real pid instead: the inner shell becomes
    # the session leader, writes its own pid, then exec's the command into it,
    # so the file holds the long-lived process (and the group-leader that
    # stop_all's `kill -- -$pid` needs).
    setsid bash -c 'echo $$ > "$1"; shift; exec "$@"' _ \
      "$RUN_DIR/$name.pid" "$@" > "$RUN_DIR/$name.log" 2>&1 &
  else
    "$@" > "$RUN_DIR/$name.log" 2>&1 &
    echo $! > "$RUN_DIR/$name.pid"
  fi
}

# ── Readiness helpers ───────────────────────────────────────────────────────

# bash's own /dev/tcp rather than nc: nc is absent from some minimal images and
# its -z flag is not portable between the BSD and OpenBSD builds.
#
# Liveness is checked as well as the port, and checked first, because the port
# alone proves nothing about *our* forward: anything else already listening
# there (another port-forward, k9s, a local postgres) keeps answering while
# kubectl exits with "address already in use", so a port-only check reports
# ready and silently hands the agent a tunnel to somewhere unknown.
wait_for_port() { # name, port, label, timeout_s
  local name="$1" port="$2" label="$3" timeout="${4:-30}" waited=0
  echo "waiting for $label on localhost:$port (up to ${timeout}s)..."
  # Let kubectl get far enough to fail before the port is first believed.
  sleep 1
  while [ "$waited" -lt "$timeout" ]; do
    if ! running "$name"; then
      echo "ERROR: $name exited. Last log lines:"
      tail -10 "$RUN_DIR/$name.log" 2>/dev/null || true
      echo "Hint: if that port is held by something else, pick another with"
      echo "      NATS_LOCAL_PORT / PG_LOCAL_PORT, and update $CONFIG to match."
      exit 1
    fi
    if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
      exec 3<&- 2>/dev/null || true
      echo "$label is ready"
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  echo "ERROR: $label did not open port $port. Last log lines:"
  tail -20 "$RUN_DIR/$name.log" 2>/dev/null || true
  exit 1
}

# The agent serves NATS, not HTTP - there is no endpoint to curl, and
# admin_port is reserved but unimplemented - so readiness is the line it logs
# once it is connected, subscribed and advertising. Bailing out early when the
# process is already gone turns an auth failure into an immediate error rather
# than a timeout.
wait_for_log() { # name, pattern, label, timeout_s
  local name="$1" pattern="$2" label="$3" timeout="${4:-60}" waited=0
  local log="$RUN_DIR/$name.log"
  echo "waiting for $label (up to ${timeout}s)..."
  while [ "$waited" -lt "$timeout" ]; do
    if grep -q "$pattern" "$log" 2>/dev/null; then
      echo "$label is ready"
      return 0
    fi
    if ! running "$name"; then
      echo "ERROR: $name exited before $label. Last log lines:"
      tail -30 "$log" 2>/dev/null || true
      exit 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  echo "ERROR: $label did not appear. Last log lines:"
  tail -30 "$log" 2>/dev/null || true
  exit 1
}

# ── Config ──────────────────────────────────────────────────────────────────

# Generated rather than required so the script is runnable with no setup. The
# broker and database hosts come from $NATS_HOST / $PG_HOST, which are the
# cluster DNS names in-cluster and localhost in forward mode. It points at the
# LEGO database deploy-postgresql.sh installs by default. Edit it, or point
# BOW_EDGE_AGENT_CONFIG elsewhere; an existing file is never overwritten.
require_certs() {
  local missing=0
  for f in "$TLS_CA" "$TLS_CERT" "$TLS_KEY"; do
    if [ ! -f "$f" ]; then
      echo "ERROR: mTLS material not found: $f"
      missing=1
    fi
  done
  if [ "$missing" = "1" ]; then
    echo "The agent authenticates with mTLS only, so it cannot start without a client"
    echo "cert. Either download the bundle from Settings -> Data Tunnels and set"
    echo "BOW_EDGE_CERT_DIR to its certs/ directory, or issue one by hand as in"
    echo ".claude/skills/data-edge-agent-sandbox/SKILL.md (CN=$ORG_ID-$EDGE_AGENT_ID)."
    exit 1
  fi
}

write_default_config() {
  [ -f "$CONFIG" ] && { echo "using existing config $CONFIG"; return; }
  cat > "$CONFIG" <<EOF
org_id: $ORG_ID
edge_agent_id: $EDGE_AGENT_ID
edge_agent_name: Local Test Agent

# Native TLS endpoint: mTLS is the agent's only authentication, and the client
# cert is exchanged in the TLS handshake. ws:// carries no cert and is refused.
tunnel_endpoint_url: $NATS_URL
tls_ca: $TLS_CA
tls_cert: $TLS_CERT
tls_key: $TLS_KEY
tls_verify: $TLS_VERIFY

connections:
  - name: lego-pg
    type: postgresql
    label: LEGO sample
    config:
      host: $PG_HOST
      port: $PG_PORT
      database: lego
      schema: public
    credentials:
      user: lego
      password: lego

  # A second on-prem source, served by the backend's own MysqlClient over
  # PYTHONPATH (the reuse path — no per-type client code in the agent).
  # Deployed by the mysql Deployment in the bow-test rig (demo/demo/demo).
  - name: demo-mysql
    type: mysql
    label: Demo MySQL
    config:
      host: $MYSQL_HOST
      port: $MYSQL_PORT
      database: demo
    credentials:
      user: demo
      password: demo

log_level: INFO
EOF
  echo "wrote default config $CONFIG"
}

# ── Main ────────────────────────────────────────────────────────────────────

case "${1:-}" in
  --stop)   stop_all; exit 0 ;;
  --status) status_all; exit 0 ;;
  "")       ;;
  *) echo "unknown flag: $1"; exit 2 ;;
esac

# --- reach the services ------------------------------------------------------
# In-cluster the ClusterIP services resolve directly, so nothing to forward. On
# a developer host they do not, so BOW_EDGE_PORT_FORWARD=1 forwards them first.
if [ "$PORT_FORWARD" = "1" ]; then
  command -v kubectl >/dev/null || { echo "ERROR: kubectl not found (unset BOW_EDGE_PORT_FORWARD to run in-cluster)"; exit 1; }

  if ! kubectl get svc nats -n "$NATS_NAMESPACE" >/dev/null 2>&1; then
    echo "ERROR: service 'nats' not found in namespace '$NATS_NAMESPACE'."
    echo "       The broker is provisioned outside this repo (cert-manager Issuer"
    echo "       'nats-ca', ConfigMap 'nats-accounts'); see"
    echo "       .claude/skills/data-edge-agent-sandbox/SKILL.md. Set NATS_NAMESPACE"
    echo "       if it lives elsewhere."
    exit 1
  fi
  if ! kubectl get svc postgresql -n "$NAMESPACE" >/dev/null 2>&1; then
    echo "ERROR: service 'postgresql' not found in namespace '$NAMESPACE'."
    echo "       Deploy it first: tools/agent/deploy-postgresql.sh"
    exit 1
  fi

  start_bg nats-forward kubectl port-forward -n "$NATS_NAMESPACE" svc/nats "$NATS_LOCAL_PORT:4222"
  start_bg pg-forward   kubectl port-forward -n "$NAMESPACE" svc/postgresql "$PG_LOCAL_PORT:5432"
  wait_for_port nats-forward "$NATS_LOCAL_PORT" "nats forward" 30
  wait_for_port pg-forward "$PG_LOCAL_PORT" "postgres forward" 30
else
  echo "in-cluster mode - reaching services directly:"
  echo "  nats:     $NATS_URL"
  echo "  postgres: $PG_HOST:$PG_PORT"
fi

# --- agent -------------------------------------------------------------------
cd "$ROOT/data_plane"
command -v uv >/dev/null || pip install uv
uv sync --frozen --extra dev

require_certs
write_default_config

# Reuse the backend's data-source clients over the tunnel (design B1/Step 5):
# put backend/ on PYTHONPATH so the agent's registry can import
# app.data_sources.clients.* for any on-prem type.
export PYTHONPATH="$ROOT/backend:${PYTHONPATH:-}"

start_bg edge-agent uv run python -m data_edge_agent --config "$CONFIG"
wait_for_log edge-agent "edge_agent.started" "edge agent" 60

echo
echo "edge agent is up:"
grep -E "nats.connected|nats.subscribed|advertised" "$RUN_DIR/edge-agent.log" | sed 's/^/  /'
echo
echo "config: $CONFIG"
echo "logs: $RUN_DIR/{nats-forward,pg-forward,edge-agent}.log"
echo "stop with: tools/agent/boot_data_edge_agent.sh --stop"

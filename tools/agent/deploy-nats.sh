#!/usr/bin/env bash
# Deploy NATS server on Kubernetes for testing.
#
# Usage:
#   tools/agent/deploy-nats.sh              # install or upgrade
#   tools/agent/deploy-nats.sh --delete     # tear down
#   tools/agent/deploy-nats.sh --status     # check status
#
# Environment:
#   NAMESPACE        target namespace (default: bow-test)
#   RELEASE_NAME     helm release name (default: nats)
#   NATS_TOKEN       auth token for data plane connections (default: bow-test-token)
set -euo pipefail

NAMESPACE="${NAMESPACE:-bow-test}"
RELEASE_NAME="${RELEASE_NAME:-nats}"
NATS_TOKEN="${NATS_TOKEN:-bow-test-token}"

# ── Helpers ─────────────────────────────────────────────────────────────────

usage() {
  echo "Usage: $0 [--delete|--status]"
  exit 1
}

ensure_namespace() {
  kubectl get namespace "$NAMESPACE" &>/dev/null || kubectl create namespace "$NAMESPACE"
}

# Print connection details for the deployed release.
#
# Values come from the live cluster rather than the defaults above, since the
# release may have been deployed with a different token or ports. Ports are read
# from the service's named ports; the token is grepped out of the config map
# because its nats.conf is not valid JSON ("server_name": $SERVER_NAME is an
# unquoted variable), so jq cannot parse it. Falls back to the configured
# defaults when the release is not deployed.
print_connection() {
  local host token client_port ws_port ws_scheme conf origin

  host="$RELEASE_NAME.$NAMESPACE.svc.cluster.local"
  conf=$(kubectl get configmap -n "$NAMESPACE" "$RELEASE_NAME-config" \
    -o jsonpath='{.data.nats\.conf}' 2>/dev/null || true)

  if [ -n "$conf" ]; then
    origin="live values from namespace '$NAMESPACE'"
    token=$(printf '%s' "$conf" | grep -o '"token"[[:space:]]*:[[:space:]]*"[^"]*"' \
      | head -1 | sed 's/.*"\([^"]*\)"$/\1/' || true)
    client_port=$(kubectl get svc -n "$NAMESPACE" "$RELEASE_NAME" \
      -o jsonpath='{.spec.ports[?(@.name=="nats")].port}' 2>/dev/null || true)
    ws_port=$(kubectl get svc -n "$NAMESPACE" "$RELEASE_NAME" \
      -o jsonpath='{.spec.ports[?(@.name=="websocket")].port}' 2>/dev/null || true)
    # the chart sets no_tls when websocket TLS is off, so the scheme is ws not wss
    if printf '%s' "$conf" | grep -q '"no_tls"[[:space:]]*:[[:space:]]*true'; then
      ws_scheme="ws"
    else
      ws_scheme="wss"
    fi
  else
    origin="configured defaults - release not deployed"
    ws_scheme="ws"
  fi

  token="${token:-$NATS_TOKEN}"
  client_port="${client_port:-4222}"
  ws_port="${ws_port:-9443}"

  echo "=== Connection ($origin) ==="
  echo ""
  echo "Host:        $host"
  echo "Client port: $client_port"
  echo "WS port:     $ws_port"
  echo "Auth token:  $token"
  echo ""
  echo "Endpoints:"
  echo "  nats://$host:$client_port"
  echo "  $ws_scheme://$host:$ws_port"
  echo ""
  # The token deliberately is not embedded as nats://<token>@host. That URI form
  # is rejected by the nats CLI shipped in nats-box (0.4.0), which wants it as a
  # flag, and the edge agent takes the URL and credentials separately anyway.
  echo "The token is passed separately, not embedded in the URL:"
  echo "  nats --server nats://$host:$client_port --token '$token' rtt"
}

# ── Commands ────────────────────────────────────────────────────────────────

do_status() {
  echo "=== Helm release ==="
  helm status "$RELEASE_NAME" -n "$NAMESPACE" 2>/dev/null || echo "Release '$RELEASE_NAME' not found in namespace '$NAMESPACE'"
  echo ""
  echo "=== Pods ==="
  kubectl get pods -n "$NAMESPACE" -l "app.kubernetes.io/instance=$RELEASE_NAME" 2>/dev/null
  echo ""
  echo "=== Services ==="
  kubectl get svc -n "$NAMESPACE" -l "app.kubernetes.io/instance=$RELEASE_NAME" 2>/dev/null
  echo ""
  print_connection
}

do_delete() {
  echo "Deleting NATS release '$RELEASE_NAME' from namespace '$NAMESPACE'..."
  helm uninstall "$RELEASE_NAME" -n "$NAMESPACE" 2>/dev/null || echo "Release not found, nothing to delete"
  echo "Done."
}

do_install() {
  ensure_namespace

  helm repo add nats https://nats-io.github.io/k8s/helm/charts/ 2>/dev/null || true
  helm repo update nats

  echo "Installing NATS '$RELEASE_NAME' in namespace '$NAMESPACE'..."

  helm upgrade --install "$RELEASE_NAME" nats/nats \
    --namespace "$NAMESPACE" \
    --set config.cluster.enabled=false \
    --set config.jetstream.enabled=false \
    --set config.websocket.enabled=true \
    --set config.websocket.port=9443 \
    --set config.merge.max_payload=67108864 \
    --set config.merge.authorization.token="$NATS_TOKEN" \
    --wait --timeout 120s

  echo ""
  echo "=== NATS deployed ==="
  echo ""

  do_status
}

# ── Main ────────────────────────────────────────────────────────────────────

case "${1:-}" in
  --delete)  do_delete ;;
  --status)  do_status ;;
  --help|-h) usage ;;
  "")        do_install ;;
  *)         usage ;;
esac

#!/usr/bin/env bash
# Deploy the bow runtime pod on Kubernetes for testing.
#
# The pod runs a small Python HTTP server. PUT uploads a tar archive: the body
# is saved under /tmp, unpacked there, and each extracted file is copied into
# /sandbox/app - keeping its path within the archive, and only when the file is
# new or its contents differ. GET serves /sandbox/app, the working directory.
# No image build and no chart - the source lives in a config map and runs on a
# stock python image.
#
# Usage:
#   tools/agent/deploy-bow-runtime.sh              # install or upgrade
#   tools/agent/deploy-bow-runtime.sh --delete     # tear down
#   tools/agent/deploy-bow-runtime.sh --status     # check status
#   tools/agent/deploy-bow-runtime.sh --start      # start the app inside the pod
#
# Environment:
#   NAMESPACE      target namespace                  (default: bow-test)
#   APP_NAME       deployment / service name         (default: bow-runtime)
#   IMAGE          container image                   (default: localhost:32000/bagofwords:dev-mcp)
#   APP_PORT       port the server listens on        (default: 9191)
#   EXPOSED_PORTS  every port published on the pod   (default: 8080 3000 9191)
#   NATS_URL       backend tunnel NATS url           (default: nats://nats.<ns>.svc.cluster.local:4222)
#   NATS_TOKEN     backend tunnel NATS token         (default: bow-test-token)
#   BOOT_SCRIPT    script --start runs in the pod    (default: tools/agent/boot_stack.sh)
#   BOOT_ARGS      arguments passed to it            (default: --dev)
#   BOOT_LOG       where its output is redirected    (default: /tmp/boot_stack.log)
#   EXPOSE_EXTERNAL  1 to add a Gateway + HTTPRoute      (default: 0)
#   GATEWAY_CLASS  gatewayClassName for the Gateway      (default: eg — Envoy Gateway)
#   GATEWAY_PORT   external HTTP listener port           (default: 80)
#   ROUTE_PORT     app service port the route targets    (default: 3000 — the frontend)
set -euo pipefail

NAMESPACE="${NAMESPACE:-bow-test}"
APP_NAME="${APP_NAME:-bow-runtime}"
IMAGE="${IMAGE:-localhost:32000/bagofwords:dev-mcp}"
APP_PORT="${APP_PORT:-9191}"
EXPOSED_PORTS="${EXPOSED_PORTS:-8080 3000 9191}"
# The backend's secure data tunnel (design B3/B4). In-cluster it reaches NATS by
# service DNS on the TCP client port 4222 (not the edge agents' websocket 9443).
NATS_URL="${NATS_URL:-nats://nats.$NAMESPACE.svc.cluster.local:4222}"
NATS_TOKEN="${NATS_TOKEN:-bow-test-token}"
BOOT_SCRIPT="${BOOT_SCRIPT:-tools/agent/boot_stack.sh}"
BOOT_ARGS="${BOOT_ARGS---dev}"
BOOT_LOG="${BOOT_LOG:-/tmp/boot_stack.log}"
# External access via the Gateway API (Envoy Gateway is the cluster's controller).
# Off by default; the app deploy turns it on. The route targets the frontend
# port (3000), which also proxies /api to the backend.
EXPOSE_EXTERNAL="${EXPOSE_EXTERNAL:-0}"
GATEWAY_CLASS="${GATEWAY_CLASS:-eg}"
GATEWAY_PORT="${GATEWAY_PORT:-80}"
ROUTE_PORT="${ROUTE_PORT:-3000}"

# Uploads land here, and it is also the server's working directory, so a file
# is read back at the same path it was written to. The config map is mounted
# read-only, so this has to be a writable volume of its own; without one the
# uploads would go to the container's ephemeral layer instead.
WORK_DIR="/sandbox/app"

# ── Helpers ─────────────────────────────────────────────────────────────────

usage() {
  echo "Usage: $0 [--delete|--status|--start]"
  exit 1
}

ensure_namespace() {
  kubectl get namespace "$NAMESPACE" &>/dev/null || kubectl create namespace "$NAMESPACE"
}

# The server source, written to a file so kubectl can build the config map with
# --from-file. Embedding it in the manifest heredoc instead would put its
# quoting and indentation at the mercy of the shell and of YAML block scalars.
write_server_source() { # path
  cat > "$1" <<'PYEOF'
#!/usr/bin/env python3
"""Upload endpoint that unpacks a tar archive into the app directory.

PUT /<name> saves the body to /tmp/<name>, extracts it to /tmp/<name>.d, and
copies every extracted file into /sandbox/app, keeping its path within the
archive. A file is copied only when it is absent from the target or its
contents differ, so unchanged files are left alone.
"""
import hashlib
import http.server
import os
import shutil
import tarfile

UPLOAD_DIR = "/tmp"
TARGET_DIR = "/sandbox/app"
CHUNK = 1024 * 1024


def _digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.digest()


def _verdict(src, dst):
    """None when the target already holds exactly this content."""
    if not os.path.exists(dst):
        return "new"
    # Size first: it settles most cases without reading either file through.
    if os.path.getsize(src) != os.path.getsize(dst):
        return "changed"
    if _digest(src) != _digest(dst):
        return "changed"
    return None


class UploadHandler(http.server.SimpleHTTPRequestHandler):
    def do_PUT(self):
        filename = os.path.basename(self.path.lstrip("/")) or "upload.tar"
        archive = os.path.join(UPLOAD_DIR, filename)
        length = int(self.headers.get("Content-Length", 0))

        os.makedirs(UPLOAD_DIR, exist_ok=True)
        # Read in chunks: an archive need not be small, and a single read() of
        # Content-Length holds the whole upload in memory at once.
        with open(archive, "wb") as f:
            remaining = length
            while remaining > 0:
                block = self.rfile.read(min(CHUNK, remaining))
                if not block:
                    break
                f.write(block)
                remaining -= len(block)

        extract_dir = archive + ".d"
        # Rebuilt every time. Leftovers from an earlier upload would otherwise
        # be walked and copied out as though they came from this archive.
        shutil.rmtree(extract_dir, ignore_errors=True)
        os.makedirs(extract_dir, exist_ok=True)

        try:
            with tarfile.open(archive, "r:*") as tar:
                # filter="data" rejects absolute paths, "..", links pointing out
                # of the tree, and device nodes. Without it a crafted archive
                # writes anywhere this process can reach.
                tar.extractall(extract_dir, filter="data")
        except (tarfile.TarError, OSError) as e:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(f"not a readable tar archive: {e}\n".encode())
            return

        added, changed, same = [], [], []
        for root, _dirs, files in os.walk(extract_dir):
            for name in files:
                src = os.path.join(root, name)
                rel = os.path.relpath(src, extract_dir)
                dst = os.path.join(TARGET_DIR, rel)
                verdict = _verdict(src, dst)
                if verdict is None:
                    same.append(rel)
                    continue
                # Recreate the archive's directory structure under the target.
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                (added if verdict == "new" else changed).append(rel)

        self.send_response(201)
        self.end_headers()
        lines = [
            f"saved {filename} ({length} bytes) to {archive}",
            f"extracted to {extract_dir}",
            f"{len(added)} new, {len(changed)} changed, {len(same)} unchanged",
        ]
        lines += [f"  + {r}" for r in sorted(added)]
        lines += [f"  ~ {r}" for r in sorted(changed)]
        lines += [f"  = {r}" for r in sorted(same)]
        self.wfile.write(("\n".join(lines) + "\n").encode())


if __name__ == "__main__":
    os.makedirs(TARGET_DIR, exist_ok=True)
    # The listener must agree with the port the service and probe use, so it
    # comes from the environment rather than being hardcoded here.
    http.server.test(HandlerClass=UploadHandler,
                     port=int(os.environ.get("APP_PORT", "9191")))
PYEOF
}

container_ports() {
  local p
  for p in $EXPOSED_PORTS; do
    printf '            - containerPort: %s\n              name: p%s\n' "$p" "$p"
  done
}

service_ports() {
  local p
  for p in $EXPOSED_PORTS; do
    printf '    - name: p%s\n      port: %s\n      targetPort: %s\n      protocol: TCP\n' "$p" "$p" "$p"
  done
}

print_connection() {
  local service host cluster_ip origin ports

  # The service name is what callers need in order to address the runtime, so
  # it is read back from the cluster rather than assumed from APP_NAME - the two
  # differ whenever the deployed release was created under another name.
  service=$(kubectl get svc "$APP_NAME" -n "$NAMESPACE" \
    -o jsonpath='{.metadata.name}' 2>/dev/null || true)

  if [ -n "$service" ]; then
    origin="live values from namespace '$NAMESPACE'"
    ports=$(kubectl get svc "$service" -n "$NAMESPACE" \
      -o jsonpath='{range .spec.ports[*]}{.port} {end}' 2>/dev/null || true)
    cluster_ip=$(kubectl get svc "$service" -n "$NAMESPACE" \
      -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)
  else
    origin="configured defaults - not deployed"
    service="$APP_NAME"
  fi
  ports="${ports:-$EXPOSED_PORTS}"
  host="$service.$NAMESPACE.svc.cluster.local"

  echo "=== Connection ($origin) ==="
  echo ""
  echo "Service:        $service"
  echo "Namespace:      $NAMESPACE"
  echo "ClusterIP:      ${cluster_ip:-<none - not deployed>}"
  echo "Host:           $host"
  echo "Exposed ports:  $ports"
  echo "HTTP listener:  $APP_PORT"
  echo ""
  echo "Upload URL - PUT a tar archive here:"
  echo "  http://$host:$APP_PORT/<archive>.tgz"
  echo ""

  # External URL, if this pod was exposed via a Gateway.
  if kubectl get gateway "$APP_NAME-gw" -n "$NAMESPACE" >/dev/null 2>&1; then
    local ext; ext="$(gateway_address)"
    if [ -n "$ext" ]; then
      local port_suffix=""
      [ "$GATEWAY_PORT" = "80" ] || port_suffix=":$GATEWAY_PORT"
      echo "External URL (from outside the cluster):"
      echo "  http://$ext$port_suffix/"
    else
      echo "External URL: Gateway '$APP_NAME-gw' has no address yet."
    fi
    echo ""
  fi
  # Only APP_PORT has a process behind it. The others are published because the
  # deployment asks for them; a connection to one is refused inside the pod, so
  # a probe or a port-forward against them will not behave like a live service.
  echo "Only $APP_PORT is served - the other ports are published but have no listener."
  echo ""
  echo "Endpoint:"
  echo "  http://$host:$APP_PORT/"
  echo ""
  echo "From your machine:"
  echo "  kubectl port-forward -n $NAMESPACE svc/$service $APP_PORT:$APP_PORT"
  echo "  tar -czf /tmp/app.tgz -C ./yourdir ."
  echo "  curl -T /tmp/app.tgz http://localhost:$APP_PORT/app.tgz  # unpack into $WORK_DIR"
  echo "  curl http://localhost:$APP_PORT/                         # list $WORK_DIR"
  echo "  curl http://localhost:$APP_PORT/path/in/archive.txt      # read one back"
}

# ── External access (Gateway API) ───────────────────────────────────────────

# Apply a Gateway + HTTPRoute so the app is reachable from outside the cluster.
# The Gateway provisions an Envoy proxy fronted by a LoadBalancer service, which
# metallb gives an address from its pool; the HTTPRoute (no hostnames = match
# any host) forwards every request to the app service's frontend port.
apply_gateway() {
  echo "=== External access (Gateway '$GATEWAY_CLASS') ==="
  kubectl apply -f - <<EOF
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: $APP_NAME-gw
  namespace: $NAMESPACE
  labels:
    app: $APP_NAME
spec:
  gatewayClassName: $GATEWAY_CLASS
  listeners:
    - name: http
      protocol: HTTP
      port: $GATEWAY_PORT
      allowedRoutes:
        namespaces:
          from: Same
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: $APP_NAME-route
  namespace: $NAMESPACE
  labels:
    app: $APP_NAME
spec:
  parentRefs:
    - name: $APP_NAME-gw
  rules:
    - backendRefs:
        - name: $APP_NAME
          port: $ROUTE_PORT
EOF

  echo ""
  echo "Waiting for the gateway to be assigned an address..."
  local addr=""
  for _ in $(seq 1 30); do
    addr=$(gateway_address)
    [ -n "$addr" ] && break
    sleep 2
  done
  if [ -n "$addr" ]; then
    echo "Gateway address: $addr"
  else
    echo "Gateway not programmed yet; check 'kubectl get gateway $APP_NAME-gw -n $NAMESPACE'."
  fi
}

# The external address the controller assigned to the Gateway (empty until it is
# programmed). Prefer a hostname if one is set, else the IP.
gateway_address() {
  kubectl get gateway "$APP_NAME-gw" -n "$NAMESPACE" \
    -o jsonpath='{.status.addresses[0].value}' 2>/dev/null || true
}

# ── Commands ────────────────────────────────────────────────────────────────

do_status() {
  echo "=== Deployment ==="
  kubectl get deployment "$APP_NAME" -n "$NAMESPACE" 2>/dev/null \
    || echo "Deployment '$APP_NAME' not found in namespace '$NAMESPACE'"
  echo ""
  echo "=== Pods ==="
  kubectl get pods -n "$NAMESPACE" -l "app=$APP_NAME" 2>/dev/null
  echo ""
  echo "=== Services ==="
  kubectl get svc -n "$NAMESPACE" -l "app=$APP_NAME" 2>/dev/null
  echo ""
  print_connection
}

do_delete() {
  echo "Deleting bow runtime '$APP_NAME' from namespace '$NAMESPACE'..."
  kubectl delete deployment,service,configmap -n "$NAMESPACE" -l "app=$APP_NAME" 2>/dev/null \
    || echo "Nothing to delete"
  # Gateway/HTTPRoute exist only when the pod was exposed; the label match is a
  # no-op otherwise.
  kubectl delete httproute,gateway -n "$NAMESPACE" -l "app=$APP_NAME" 2>/dev/null || true
  echo ""
  echo "Uploads lived in an emptyDir and are gone with the pod."
  echo "Done."
}

# Launch the app inside the pod and return immediately.
#
# kubectl exec waits for the remote command to finish, so the boot script is
# backgrounded on the far side with its streams redirected to a file. Both parts
# matter: exec cannot return while the child still holds its stdout, so the
# redirect is what lets this return rather than stream forever. nohup and
# </dev/null keep the child alive once the wrapping shell is gone.
do_start() {
  local pod pid

  pod=$(kubectl get pod -n "$NAMESPACE" -l "app=$APP_NAME" \
    --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  if [ -z "$pod" ]; then
    echo "No running pod for '$APP_NAME' in namespace '$NAMESPACE'."
    echo "Deploy it first: $0"
    exit 1
  fi

  if ! kubectl exec -n "$NAMESPACE" "$pod" -- test -f "$WORK_DIR/$BOOT_SCRIPT" 2>/dev/null; then
    echo "$BOOT_SCRIPT not found under $WORK_DIR in pod $pod."
    echo "Upload the sources first - PUT a tar archive at the upload URL shown by --status."
    exit 1
  fi

  echo "Starting $BOOT_SCRIPT $BOOT_ARGS in pod $pod ..."
  pid=$(kubectl exec -n "$NAMESPACE" "$pod" -- sh -c \
    "cd \"$WORK_DIR\" && nohup bash \"$BOOT_SCRIPT\" $BOOT_ARGS > \"$BOOT_LOG\" 2>&1 < /dev/null & echo \$!")

  echo ""
  echo "Started in the background - not waiting for it to exit."
  echo "  pod: $pod"
  echo "  pid: $pid"
  echo "  log: $BOOT_LOG"
  echo ""
  echo "Follow it with:"
  echo "  kubectl exec -n $NAMESPACE $pod -- tail -f $BOOT_LOG"
}

do_install() {
  ensure_namespace

  local src checksum
  src="$(mktemp -t bow-runtime-server)"
  # shellcheck disable=SC2064
  trap "rm -f '$src'" EXIT
  write_server_source "$src"

  echo "Deploying bow runtime '$APP_NAME' in namespace '$NAMESPACE' (image $IMAGE)..."

  # --dry-run | apply rather than `create`, so a redeploy updates the existing
  # config map instead of failing on AlreadyExists.
  kubectl create configmap "$APP_NAME-src" \
    --from-file=server.py="$src" \
    -n "$NAMESPACE" --dry-run=client -o yaml \
    | kubectl label --local -f - "app=$APP_NAME" -o yaml --dry-run=client \
    | kubectl apply -f -

  # A config map change does not restart the pods on its own. Stamping its
  # checksum on the pod template makes an edited server.py roll out. cksum is
  # POSIX, unlike sha256sum/shasum which differ between Linux and macOS.
  checksum="$(cksum < "$src" | awk '{print $1}')"

  kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $APP_NAME
  namespace: $NAMESPACE
  labels:
    app: $APP_NAME
spec:
  replicas: 1
  selector:
    matchLabels:
      app: $APP_NAME
  template:
    metadata:
      labels:
        app: $APP_NAME
      annotations:
        bow.dev/src-checksum: "$checksum"
    spec:
      containers:
        - name: server
          image: $IMAGE
          command: ["python3", "/src/server.py"]
          workingDir: $WORK_DIR
          env:
            - name: APP_PORT
              value: "$APP_PORT"
            - name: NATS_URL
              value: "$NATS_URL"
            - name: NATS_TOKEN
              value: "$NATS_TOKEN"
          ports:
$(container_ports)
          volumeMounts:
            - name: src
              mountPath: /src
              readOnly: true
            - name: work
              mountPath: $WORK_DIR
      volumes:
        - name: src
          configMap:
            name: $APP_NAME-src
        - name: work
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: $APP_NAME
  namespace: $NAMESPACE
  labels:
    app: $APP_NAME
spec:
  type: ClusterIP
  selector:
    app: $APP_NAME
  ports:
$(service_ports)
EOF

  echo ""
  echo "Waiting for rollout..."
  kubectl rollout status deployment/"$APP_NAME" -n "$NAMESPACE" --timeout=120s

  if [ "$EXPOSE_EXTERNAL" = "1" ]; then
    echo ""
    apply_gateway
  fi

  echo ""
  echo "=== bow runtime deployed ==="
  echo ""

  do_status
}

# ── Main ────────────────────────────────────────────────────────────────────

case "${1:-}" in
  --delete)  do_delete ;;
  --status)  do_status ;;
  --start)   do_start ;;
  --help|-h) usage ;;
  "")        do_install ;;
  *)         usage ;;
esac

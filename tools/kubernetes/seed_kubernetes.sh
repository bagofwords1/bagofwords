#!/usr/bin/env bash
# Stand up the Kubernetes connector sandbox: a kind cluster with metrics-server,
# the read-only RBAC from rbac.yaml, and seeded workloads in healthy, crash-
# looping, OOM-killed, pending (storage + scheduling) and failing-cronjob
# states, plus a sample CRD. Prints the path of the access file on the LAST
# line (the tools/<type> seed convention) — paste it into the connect form.
#
#   tools/kubernetes/seed_kubernetes.sh          # create/refresh
#   tools/kubernetes/seed_kubernetes.sh --stop   # tear down
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CLUSTER="bow-k8s"
OUT_DIR="${BOW_AGENT_DIR:-/tmp/bow-agent}"
ACCESS_FILE="$OUT_DIR/kubernetes-access.yaml"

if [ "${1:-}" = "--stop" ]; then
  kind delete cluster --name "$CLUSTER"
  exit 0
fi

mkdir -p "$OUT_DIR"
if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  kind create cluster --config "$HERE/kind-config.yaml" --wait 120s
fi
kubectl config use-context "kind-$CLUSTER" >/dev/null

# metrics-server (needs --kubelet-insecure-tls on kind's self-signed kubelets)
if ! kubectl -n kube-system get deploy metrics-server >/dev/null 2>&1; then
  kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
  kubectl -n kube-system patch deploy metrics-server --type=json \
    -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
fi

kubectl apply -f "$HERE/rbac.yaml"
# The CRD must be established before its instances apply, so it goes first.
kubectl apply -f "$HERE/manifests/50-crd.yaml"
kubectl wait --for=condition=established --timeout=60s crd/widgets.example.com
kubectl apply -f "$HERE/manifests/"

echo "waiting for workloads to reach their intended states…"
kubectl -n payments rollout status deploy/checkout --timeout=180s
kubectl -n inventory rollout status deploy/catalog-api --timeout=180s
kubectl -n kube-system rollout status deploy/metrics-server --timeout=180s || true
# The failure states: wait until the crash-looper has restarted at least once.
for _ in $(seq 1 60); do
  r="$(kubectl -n payments get pods -l app=fraud-scorer -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' 2>/dev/null || echo 0)"
  [ "${r:-0}" -ge 1 ] && break; sleep 3
done

bash "$HERE/print_access_file.sh" > "$ACCESS_FILE"
echo "kubectl --kubeconfig $ACCESS_FILE get nodes   # verifies the access file"
echo "$ACCESS_FILE"

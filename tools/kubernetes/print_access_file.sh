#!/usr/bin/env bash
# Prints the Bag of Words cluster access file for the bagofwords-reader service account.
set -euo pipefail
NS="${BOW_NAMESPACE:-bagofwords}"; SECRET="${BOW_SECRET:-bagofwords-reader-token}"
TOKEN=""
for _ in $(seq 1 30); do
  TOKEN="$(kubectl -n "$NS" get secret "$SECRET" -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)"
  [ -n "$TOKEN" ] && break; sleep 1
done
[ -n "$TOKEN" ] || { echo "no token in secret $NS/$SECRET yet — apply rbac.yaml first" >&2; exit 1; }
CA="$(kubectl -n "$NS" get secret "$SECRET" -o jsonpath='{.data.ca\.crt}')"
SERVER="$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.server}')"
cat <<EOF
apiVersion: v1
kind: Config
clusters:
- name: bagofwords
  cluster:
    server: ${SERVER}
    certificate-authority-data: ${CA}
users:
- name: bagofwords-reader
  user:
    token: ${TOKEN}
contexts:
- name: bagofwords
  context: {cluster: bagofwords, user: bagofwords-reader}
current-context: bagofwords
EOF

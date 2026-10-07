#!/usr/bin/env bash
# Installs agent-sandbox (https://agent-sandbox.sigs.k8s.io/docs) on the current kubectl context:
# controller + extensions, sandbox router, Envoy Gateway in front of the router,
# and the Python sandbox template + warm pool that sandbox_client.py claims from.
#
# Prerequisite: the cluster must hand out LoadBalancer IPs for the Gateway
# (on microk8s: `microk8s enable metallb:<ip-range>`).
set -euo pipefail

AGENT_SANDBOX_VERSION="${AGENT_SANDBOX_VERSION:-v1.0.5}"
ENVOY_GATEWAY_VERSION="${ENVOY_GATEWAY_VERSION:-v1.9.2}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm upgrade --install eg oci://docker.io/envoyproxy/gateway-helm \
  --version "${ENVOY_GATEWAY_VERSION}" \
  -n envoy-gateway-system --create-namespace --wait

kubectl apply -f "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VERSION}/sandbox-with-extensions.yaml"
kubectl -n agent-sandbox-system rollout status deployment/agent-sandbox-controller --timeout=90s

kubectl apply -f "${HERE}/router.yaml"
kubectl -n agent-sandbox-system rollout status deployment/sandbox-router-deployment --timeout=90s

kubectl apply -f "${HERE}/gateway.yaml"
kubectl -n agent-sandbox-system wait gateway/eg --for=condition=Programmed --timeout=120s

kubectl apply -f "${HERE}/python-sandbox-template.yaml"
kubectl apply -f "${HERE}/python-sandbox-warmpool.yaml"

echo "Gateway address: $(kubectl -n agent-sandbox-system get gateway eg -o jsonpath='{.status.addresses[0].value}')"

source 01-exports.sh

# Install the Agent Sandbox controller and its CRDs
# on your cluster before installing the OpenShell Helm chart.
export VERSION=v0.5.2
kubectl apply \
 -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${VERSION}/sandbox.yaml \
 --kubeconfig="${KIND_KUBECONFIG}"

# Patch agent-sandbox-controller role and add
# permissions for dealing with events 
kubectl patch role agent-sandbox-controller \
  --kubeconfig="${KIND_KUBECONFIG}" \
  -n agent-sandbox-system \
  --type='json' \
  -p='[{"op": "add", "path": "/rules/-", "value": {"apiGroups": ["", "events.k8s.io"], "resources": ["events"], "verbs": ["create", "patch", "update"]}}]'

# Envoy Gateway installs the Gateway API CRDs and controller
helm upgrade -i eg \
  oci://docker.io/envoyproxy/gateway-helm \
  --version v1.8.1 \
  --namespace envoy-gateway-system \
  --create-namespace \
  --wait \
  --kubeconfig="${KIND_KUBECONFIG}"

kubectl --kubeconfig="${KIND_KUBECONFIG}" apply -f - <<'EOF'
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata:
  name: eg
spec:
  controllerName: gateway.envoyproxy.io/gatewayclass-controller
EOF

kubectl create namespace openshell --kubeconfig="${KIND_KUBECONFIG}" 2>/dev/null || true

kubectl --kubeconfig="${KIND_KUBECONFIG}" apply -f - <<'EOF'
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: BackendTrafficPolicy
metadata:
  name: openshell-timeout
  namespace: openshell
spec:
  targetRefs:
    - group: gateway.networking.k8s.io
      kind: GRPCRoute
      name: openshell
  timeout:
    http:
      requestTimeout: "600s"
      connectionIdleTimeout: "600s"
EOF


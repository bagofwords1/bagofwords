source 01-exports.sh

SVC_NAME=$(kubectl get svc -A \
  --kubeconfig="${KIND_KUBECONFIG}" \
  -l gateway.envoyproxy.io/owning-gateway-name=openshell \
  -o jsonpath='{.items[0].metadata.name}')
SVC_NS=$(kubectl get svc -A \
  --kubeconfig="${KIND_KUBECONFIG}" \
  -l gateway.envoyproxy.io/owning-gateway-name=openshell \
  -o jsonpath='{.items[0].metadata.namespace}')

EXTERNAL_HOST=$(kubectl get svc "${SVC_NAME}" -n "${SVC_NS}" \
  --kubeconfig="${KIND_KUBECONFIG}" \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}{.status.loadBalancer.ingress[0].hostname}')

GATEWAY_NAME="production"
openshell gateway remove "${GATEWAY_NAME}" 2>/dev/null || true
sleep 3 # to make sure gateway is ready
openshell gateway add "http://${EXTERNAL_HOST}" --name "${GATEWAY_NAME}"
openshell status

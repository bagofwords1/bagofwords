source 01-exports.sh

helm upgrade --install openshell \
  oci://ghcr.io/nvidia/openshell/helm-chart \
  --create-namespace \
  --namespace openshell \
  -f values.yaml \
  --kubeconfig="${KIND_KUBECONFIG}"

#echo "Waiting for gateway service with external address..."
#until kubectl get svc -A \
#  --kubeconfig="${KIND_KUBECONFIG}" \
#  -l gateway.envoyproxy.io/owning-gateway-name=openshell \
#  -o jsonpath='{.items[0].status.loadBalancer.ingress[0].ip}{.items[0].status.loadBalancer.ingress[0].hostname}' 2>/dev/null | grep -q .; do
#  sleep 2
#done

#SVC_NAME=$(kubectl get svc -A \
#  --kubeconfig="${KIND_KUBECONFIG}" \
#  -l gateway.envoyproxy.io/owning-gateway-name=openshell \
#  -o jsonpath='{.items[0].metadata.name}')
#
#SVC_NS=$(kubectl get svc -A \
#  --kubeconfig="${KIND_KUBECONFIG}" \
#  -l gateway.envoyproxy.io/owning-gateway-name=openshell \
#  -o jsonpath='{.items[0].metadata.namespace}')
#
#PORT_NUM=$(kubectl get svc "${SVC_NAME}" -n "${SVC_NS}" \
#  --kubeconfig="${KIND_KUBECONFIG}" \
#  -o jsonpath='{.spec.ports[?(@.name=="http-80")].port}')

#kubectl patch svc "${SVC_NAME}" -n "${SVC_NS}" \
#  --kubeconfig="${KIND_KUBECONFIG}" \
#  -p="{\"spec\":{\"ports\":[{\"port\":${PORT_NUM},\"nodePort\":30002}]}}"


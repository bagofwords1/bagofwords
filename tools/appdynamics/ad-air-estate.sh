#!/bin/bash
# Launch the ACME-Air multi-tier Java demo estate against a real AppDynamics
# controller, using the official appdynamics/ad-air-java-services image (one
# Spring Boot jar; each container instance becomes a different tier via the
# agent's tierName) and the current appdynamics/java-agent.
#
# Produces a 7-tier service map (web-api → auth-services/api-services →
# flight-services; approval-services → sap-services; ticket-services) plus a
# load container. DB/queue calls inside the jar are simulated sleeps — this
# estate exercises multi-tier HTTP topology, not JDBC backends.
#
# Required env:
#   APPD_CONTROLLER_HOST   e.g. cloudXXXX.saas.appdynamics.com
#   APPD_ACCOUNT_NAME      e.g. cloudXXXX (on-prem: customer1)
#   APPD_ACCESS_KEY        the account access key (License → Account)
# Optional env:
#   APPD_APP_NAME (default ACME-Air), APPD_PROXY_HOST/APPD_PROXY_PORT
#   (an egress proxy the *agents* should use; on a sandbox whose proxy is
#   loopback-only, run a TCP relay on the docker bridge gateway first)
#
# Setup performed here:
#   docker network create adair          # service-name DNS between tiers
#   agent extracted from appdynamics/java-agent:latest into ./javaagent
set -euo pipefail
: "${APPD_CONTROLLER_HOST:?}" "${APPD_ACCOUNT_NAME:?}" "${APPD_ACCESS_KEY:?}"
APP="${APPD_APP_NAME:-ACME-Air}"
DIR="$(cd "$(dirname "$0")" && pwd)"
IMG="appdynamics/ad-air-java-services:latest"

docker network inspect adair >/dev/null 2>&1 || docker network create adair
if [ ! -f "$DIR/javaagent/javaagent.jar" ]; then
  docker pull -q appdynamics/java-agent:latest
  id=$(docker create appdynamics/java-agent:latest)
  mkdir -p "$DIR/javaagent"
  docker cp "$id:/opt/appdynamics/." "$DIR/javaagent/"
  docker rm "$id" >/dev/null
fi
docker pull -q "$IMG"

PROXY_OPTS=""
if [ -n "${APPD_PROXY_HOST:-}" ]; then
  PROXY_OPTS="-Dappdynamics.http.proxyHost=$APPD_PROXY_HOST -Dappdynamics.http.proxyPort=${APPD_PROXY_PORT:-3128}"
fi

TIERS=(web-api auth-services api-services flight-services approval-services sap-services ticket-services)
for tier in "${TIERS[@]}"; do
  docker rm -f "$tier" >/dev/null 2>&1 || true
  docker run -d --name "$tier" --network adair --network-alias "$tier" \
    --memory 700m -v "$DIR/javaagent:/appd:ro" \
    -e JAVA_OPTS="-javaagent:/appd/javaagent.jar \
-Dappdynamics.agent.applicationName=$APP \
-Dappdynamics.agent.tierName=$tier \
-Dappdynamics.agent.nodeName=$tier-node-1 \
-Dappdynamics.controller.hostName=$APPD_CONTROLLER_HOST \
-Dappdynamics.controller.port=443 \
-Dappdynamics.controller.ssl.enabled=true \
-Dappdynamics.agent.accountName=$APPD_ACCOUNT_NAME \
-Dappdynamics.agent.accountAccessKey=$APPD_ACCESS_KEY \
$PROXY_OPTS" \
    "$IMG"
  echo "started $tier"
  sleep 3
done

docker rm -f adair-load >/dev/null 2>&1 || true
docker run -d --name adair-load --network adair --entrypoint /bin/bash "$IMG" -c '
while true; do
  for path in "web-api:8080/web-api/searchForFlights" "web-api:8080/web-api/getFlightDetails" \
              "web-api:8080/web-api/getPromotions" "web-api:8080/web-api/getAirportDetails" \
              "web-api:8080/login/user1" \
              "approval-services:8080/approval-services/bookFlight" \
              "approval-services:8080/approval-services/reserveFlight" \
              "approval-services:8080/approval-services/managerApproval" \
              "approval-services:8080/approval-services/paymentIssued" \
              "ticket-services:8080/ticket-services/ticketIssued" \
              "ticket-services:8080/ticket-services/bookFlight"; do
    wget -qO- --timeout=15 "http://$path" >/dev/null 2>&1
    sleep 1
  done
done'
echo "load generator started; tiers/BTs/flows appear on the controller within ~10 minutes"

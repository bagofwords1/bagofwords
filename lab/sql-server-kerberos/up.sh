#!/usr/bin/env bash
# Bring the Kerberos lab up and run the delegation test suite.
set -euo pipefail
cd "$(dirname "$0")"

echo "== building images =="
docker compose build dc runner

echo "== starting DC + SQL Server 2022/2019 =="
docker compose up -d dc sql2022 sql2019

echo "== waiting for the DC to finish provisioning (keytabs) =="
for i in $(seq 1 60); do
    if docker compose exec -T dc test -f /keytabs/svc-bow.keytab 2>/dev/null; then
        echo "   DC ready."
        break
    fi
    sleep 3
done

echo "== waiting for SQL Server instances to accept AD logins (Tier B; ~60-120s) =="
sleep 60

echo "== running delegation tests (runner) =="
docker compose run --rm runner "$@"

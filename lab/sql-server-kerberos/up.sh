#!/usr/bin/env bash
# Bring the Kerberos lab up and run the delegation test suite.
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE="${BOWLAB_ENV_FILE:-.lab.env}"
if [ ! -f "${ENV_FILE}" ]; then
    base_image="ubuntu:24.04"
    aux_platform="linux/amd64"
    install_odbc="1"
    # SQL Server is amd64-only, but the DC and runner can stay native on an
    # Apple Silicon developer machine when the production BOW image is cached.
    # That image already contains ODBC 18, so no network package install is
    # needed for the runner in this path.
    if [ "$(docker info --format '{{.Architecture}}' 2>/dev/null || true)" = "arm64" ] \
        && docker image inspect bagofwords/bagofwords:latest >/dev/null 2>&1; then
        base_image="bagofwords/bagofwords:latest"
        aux_platform="linux/arm64"
        install_odbc="0"
    fi
    umask 077
    {
        echo "BOWLAB_ADMIN_PASS=Bowlab_Admin_Aa1_$(openssl rand -hex 12)"
        echo "BOWLAB_USER_PASS=Bowlab_User_Aa1_$(openssl rand -hex 12)"
        echo "BOWLAB_SVC_PASS=Bowlab_Svc_Aa1_$(openssl rand -hex 12)"
        echo "BOWLAB_SA_PASSWORD=Bowlab_Sa_Aa1_$(openssl rand -hex 12)"
        echo "BOWLAB_BASE_IMAGE=${base_image}"
        echo "BOWLAB_AUX_PLATFORM=${aux_platform}"
        echo "BOWLAB_INSTALL_ODBC=${install_odbc}"
        echo "BOWLAB_SQL_DATA_DIR=./.state/mssql"
    } > "${ENV_FILE}"
    echo "== generated lab-only credentials in ${ENV_FILE} =="
fi

compose() {
    docker compose --env-file "${ENV_FILE}" "$@"
}

echo "== building images =="
compose build dc runner

echo "== starting DC + SQL Server 2022 =="
compose up -d dc sql2022

echo "== waiting for the DC to finish provisioning (keytabs) =="
for i in $(seq 1 60); do
    if compose exec -T dc test -f /keytabs/svc-bow.keytab 2>/dev/null; then
        echo "   DC ready."
        break
    fi
    sleep 3
done

echo "== waiting for SQL Server AD initialization =="
ready=false
for i in $(seq 1 90); do
    if compose exec -T sql2022 test -f /var/opt/mssql/.bowlab-ready 2>/dev/null; then
        ready=true
        echo "   SQL Server ready."
        break
    fi
    sleep 2
done
if [ "${ready}" != "true" ]; then
    echo "SQL Server did not become ready; recent logs:"
    compose logs --no-color --tail=120 sql2022
    exit 1
fi

echo "== running delegation tests (runner) =="
compose run --rm runner "$@"

#!/usr/bin/env bash
# Tear the Kerberos lab down. Preserve AD/keytabs unless --reset is requested.
set -euo pipefail
cd "$(dirname "$0")"
ENV_FILE="${BOWLAB_ENV_FILE:-.lab.env}"
if [ ! -f "${ENV_FILE}" ]; then
    echo "No ${ENV_FILE}; nothing to stop."
    exit 0
fi
if [ "${1:-}" = "--reset" ]; then
    docker compose --env-file "${ENV_FILE}" --profile manual down -v --remove-orphans
else
    docker compose --env-file "${ENV_FILE}" --profile manual down --remove-orphans
fi

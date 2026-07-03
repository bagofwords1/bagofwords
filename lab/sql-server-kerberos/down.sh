#!/usr/bin/env bash
# Tear the Kerberos lab down (containers + volumes).
set -euo pipefail
cd "$(dirname "$0")"
docker compose --profile manual down -v --remove-orphans

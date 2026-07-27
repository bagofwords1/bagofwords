#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 -c <compose-command> -f <docker-compose-file>"
  echo "  -c  Docker compose command (e.g. 'docker compose' or 'docker-compose')"
  echo "  -f  Path to docker-compose.yaml file"
  exit 1
}

COMPOSE_CMD=""
COMPOSE_FILE=""

while getopts "c:f:" opt; do
  case "$opt" in
    c) COMPOSE_CMD="$OPTARG" ;;
    f) COMPOSE_FILE="$OPTARG" ;;
    *) usage ;;
  esac
done

[[ -z "$COMPOSE_CMD" || -z "$COMPOSE_FILE" ]] && usage

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "Error: file not found: $COMPOSE_FILE"
  exit 1
fi

COMPOSE="$COMPOSE_CMD -f $COMPOSE_FILE"

echo "$(date '+%Y-%m-%d %H:%M:%S') Pulling latest images..."
$COMPOSE pull

echo "$(date '+%Y-%m-%d %H:%M:%S') Recreating containers with updated images..."
$COMPOSE up -d

echo "$(date '+%Y-%m-%d %H:%M:%S') Removing unused images..."
docker image prune -f

echo "$(date '+%Y-%m-%d %H:%M:%S') Done."

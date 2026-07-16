#!/usr/bin/env bash
# Deploy PostgreSQL on Kubernetes for testing (Bitnami Helm chart).
#
# Usage:
#   tools/agent/deploy-postgresql.sh              # install or upgrade
#   tools/agent/deploy-postgresql.sh --delete     # tear down
#   tools/agent/deploy-postgresql.sh --status     # check status
#
# Environment:
#   NAMESPACE        target namespace (default: bow-test)
#   RELEASE_NAME     helm release name (default: postgresql)
#   PG_PASSWORD           app user password (default: lego)
#   PG_POSTGRES_PASSWORD  superuser password (default: lego)
#   PG_DATABASE           database name (default: lego)
#   PG_USERNAME           database user (default: lego)
#   PG_STORAGE            PVC size (default: 5Gi)
#   LEGO_REIMPORT         set to 1 to drop and reload the LEGO tables
set -euo pipefail

NAMESPACE="${NAMESPACE:-bow-test}"
RELEASE_NAME="${RELEASE_NAME:-postgresql}"
PG_PASSWORD="${PG_PASSWORD:-lego}"
PG_DATABASE="${PG_DATABASE:-lego}"
PG_USERNAME="${PG_USERNAME:-lego}"
PG_STORAGE="${PG_STORAGE:-5Gi}"
PG_POSTGRES_PASSWORD="${PG_POSTGRES_PASSWORD:-lego}"
CHART_VERSION="16.3.2"

LEGO_DUMP_URL="https://raw.githubusercontent.com/neondatabase/postgres-sample-dbs/refs/heads/main/lego.sql"
LEGO_TABLES="public.lego_colors, public.lego_inventories, public.lego_inventory_parts,
             public.lego_inventory_sets, public.lego_part_categories, public.lego_parts,
             public.lego_sets, public.lego_themes"

# ── Helpers ─────────────────────────────────────────────────────────────────

usage() {
  echo "Usage: $0 [--delete|--status]"
  exit 1
}

ensure_namespace() {
  kubectl get namespace "$NAMESPACE" &>/dev/null || kubectl create namespace "$NAMESPACE"
}

# Percent-encode a string for use in the userinfo part of a connection URI.
urlencode() {
  local s="$1" i c out=""
  for (( i = 0; i < ${#s}; i++ )); do
    c="${s:i:1}"
    case "$c" in
      [a-zA-Z0-9.~_-]) out+="$c" ;;
      *)               out+=$(printf '%%%02X' "'$c") ;;
    esac
  done
  printf '%s' "$out"
}

# Print connection details for the deployed release.
#
# Credentials come from the live cluster rather than the PG_* defaults above:
# the release may have been deployed with different values, and the chart keeps
# the password from a retained PVC when a release is reinstalled, so the secret
# is the only reliable source. Falls back to the defaults when not deployed.
print_connection() {
  local host user db pass postgres_pass origin user_enc pass_enc

  host="$RELEASE_NAME.$NAMESPACE.svc.cluster.local"
  pass=$(kubectl get secret -n "$NAMESPACE" "$RELEASE_NAME" \
    -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null || true)

  if [ -n "$pass" ]; then
    origin="live values from namespace '$NAMESPACE'"
    postgres_pass=$(kubectl get secret -n "$NAMESPACE" "$RELEASE_NAME" \
      -o jsonpath='{.data.postgres-password}' 2>/dev/null | base64 -d 2>/dev/null || true)
    user=$(kubectl get statefulset -n "$NAMESPACE" "$RELEASE_NAME" \
      -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="POSTGRES_USER")].value}' 2>/dev/null || true)
    db=$(kubectl get statefulset -n "$NAMESPACE" "$RELEASE_NAME" \
      -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="POSTGRES_DATABASE")].value}' 2>/dev/null || true)
  else
    origin="configured defaults - release not deployed"
  fi

  user="${user:-$PG_USERNAME}"
  db="${db:-$PG_DATABASE}"
  pass="${pass:-$PG_PASSWORD}"
  postgres_pass="${postgres_pass:-$PG_POSTGRES_PASSWORD}"

  user_enc=$(urlencode "$user")
  pass_enc=$(urlencode "$pass")

  echo "=== Connection ($origin) ==="
  echo ""
  echo "Host:      $host"
  echo "Port:      5432"
  echo "Database:  $db"
  echo "User:      $user"
  echo "Password:  $pass"
  echo "Superuser: postgres / $postgres_pass"
  echo ""
  echo "Connection string:"
  echo "  postgresql://$user_enc:$pass_enc@$host:5432/$db"
}

# Run psql inside the primary pod as the app user.
pg_psql() {
  kubectl exec -i -n "$NAMESPACE" "$RELEASE_NAME-0" -- \
    env PGPASSWORD="$PG_PASSWORD" psql -U "$PG_USERNAME" -d "$PG_DATABASE" "$@"
}

# Load the LEGO sample dataset into the app database.
#
# do_install is re-runnable (helm upgrade --install), so the import is skipped
# when the tables are already there rather than failing on duplicate objects.
# The dump is streamed from the host into the pod, so nothing has to be staged
# on the PVC. It carries no CREATE DATABASE/ROLE or ownership statements, so it
# lands in whatever database psql connects to.
import_lego() {
  local present

  echo "=== LEGO dataset ==="

  present=$(pg_psql -tAc "select to_regclass('public.lego_sets') is not null" 2>/dev/null || true)

  if [ "$present" = "t" ]; then
    if [ "${LEGO_REIMPORT:-0}" != "1" ]; then
      echo "Already present - skipping import (set LEGO_REIMPORT=1 to reload)."
      return
    fi
    echo "LEGO_REIMPORT=1 - dropping existing lego_* tables..."
    pg_psql -q -v ON_ERROR_STOP=1 -c "DROP TABLE IF EXISTS $LEGO_TABLES CASCADE;"
  fi

  echo "Importing from $LEGO_DUMP_URL ..."
  curl -fsSL "$LEGO_DUMP_URL" | pg_psql -q -o /dev/null -v ON_ERROR_STOP=1

  pg_psql -q -c "ANALYZE;"
  echo "Imported:"
  pg_psql -tAF' ' -c \
    "select relname, n_live_tup from pg_stat_user_tables where relname like 'lego%' order by relname" \
    | awk '{ printf "  %-22s %8s rows\n", $1, $2 }'
}

# ── Commands ────────────────────────────────────────────────────────────────

do_status() {
  echo "=== Helm release ==="
  helm status "$RELEASE_NAME" -n "$NAMESPACE" 2>/dev/null || echo "Release '$RELEASE_NAME' not found in namespace '$NAMESPACE'"
  echo ""
  echo "=== Pods ==="
  kubectl get pods -n "$NAMESPACE" -l "app.kubernetes.io/instance=$RELEASE_NAME" 2>/dev/null
  echo ""
  echo "=== Services ==="
  kubectl get svc -n "$NAMESPACE" -l "app.kubernetes.io/instance=$RELEASE_NAME" 2>/dev/null
  echo ""
  print_connection
}

do_delete() {
  echo "Deleting PostgreSQL release '$RELEASE_NAME' from namespace '$NAMESPACE'..."
  helm uninstall "$RELEASE_NAME" -n "$NAMESPACE" 2>/dev/null || echo "Release not found, nothing to delete"
  echo ""
  echo "Deleting the PVC - this destroys the database contents permanently:"
  kubectl delete pvc -n "$NAMESPACE" -l "app.kubernetes.io/instance=$RELEASE_NAME"
  echo "Done."
}

do_install() {
  ensure_namespace

  helm repo add bitnami https://charts.bitnami.com/bitnami 2>/dev/null || true
  helm repo update bitnami

  echo "Installing PostgreSQL '$RELEASE_NAME' (chart $CHART_VERSION) in namespace '$NAMESPACE'..."

  helm upgrade --install "$RELEASE_NAME" bitnami/postgresql \
    --namespace "$NAMESPACE" \
    --version "$CHART_VERSION" \
    --set auth.postgresPassword="$PG_POSTGRES_PASSWORD" \
    --set auth.username="$PG_USERNAME" \
    --set auth.password="$PG_PASSWORD" \
    --set auth.database="$PG_DATABASE" \
    --set primary.persistence.size="$PG_STORAGE" \
    --set primary.resources.requests.memory=256Mi \
    --set primary.resources.requests.cpu=100m \
    --set primary.resources.limits.memory=512Mi \
    --set primary.resources.limits.cpu=500m \
    --set global.security.allowInsecureImages=true \
    --set image.repository=bitnamilegacy/postgresql \
    --wait --timeout 180s

  echo ""
  import_lego

  echo ""
  echo "=== PostgreSQL deployed ==="
  echo ""

  do_status
}

# ── Main ────────────────────────────────────────────────────────────────────

case "${1:-}" in
  --delete)  do_delete ;;
  --status)  do_status ;;
  --help|-h) usage ;;
  "")        do_install ;;
  *)         usage ;;
esac

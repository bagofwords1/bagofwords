# Source this inside a DevBox (BOW_SANDBOX_DEVBOX=1) before running tools/agent
# scripts:
#
#   source tools/agent/devbox_env.sh
#
# A DevBox is provisioned with its own admin login (not admin@example.com /
# Password123!). This exports it under every name the agent scripts read:
#   BOW_ADMIN_EMAIL / BOW_ADMIN_PASSWORD   seed_org.py, setup_*_llm.py, most *.py
#   BOW_EMAIL / BOW_PASSWORD               sweep.mjs and most *.mjs drivers
# and BOW_SQLITE_DB, the app's SQLite file (empty when the DevBox runs on
# PostgreSQL). Outside a DevBox it does nothing. See AGENTS.md, "DevBox".
if [ "${BOW_SANDBOX_DEVBOX:-0}" = "1" ]; then
  _bow_ws="${BOW_WORKSPACE:-/app/workspace}"
  # stack.env carries BOW_DATABASE_URL for shells that did not source it.
  if [ -z "${BOW_DATABASE_URL:-}" ] && [ -r "$_bow_ws/run/stack.env" ]; then
    set -a; . "$_bow_ws/run/stack.env"; set +a
  fi
  # provision.env is what provisioning logged in with on this boot; the data/
  # files are the fallback (they are not rewritten when the password comes from
  # the shared bow-sandbox-config Secret).
  if [ -r "$_bow_ws/run/provision.env" ]; then
    eval "$(set -a; . "$_bow_ws/run/provision.env"; \
            printf 'BOW_ADMIN_EMAIL=%q\nBOW_ADMIN_PASSWORD=%q\n' "$BOW_ADMIN_EMAIL" "$BOW_ADMIN_PASSWORD")"
  elif [ -r "$_bow_ws/data/admin.email" ] && [ -r "$_bow_ws/data/admin.password" ]; then
    BOW_ADMIN_EMAIL="$(tr -d '\r\n' < "$_bow_ws/data/admin.email")"
    BOW_ADMIN_PASSWORD="$(tr -d '\r\n' < "$_bow_ws/data/admin.password")"
  else
    echo "devbox_env.sh: no admin login under $_bow_ws (has the DevBox finished booting?)" >&2
  fi
  if [ -n "${BOW_ADMIN_EMAIL:-}" ]; then
    export BOW_ADMIN_EMAIL BOW_ADMIN_PASSWORD
    export BOW_EMAIL="$BOW_ADMIN_EMAIL" BOW_PASSWORD="$BOW_ADMIN_PASSWORD"
  fi
  case "${BOW_DATABASE_URL:-}" in
    sqlite:///*) export BOW_SQLITE_DB="${BOW_DATABASE_URL#sqlite:///}" ;;
    *) export BOW_SQLITE_DB="" ;;
  esac
  unset _bow_ws
fi

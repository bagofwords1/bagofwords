#!/usr/bin/env bash
# Run an arbitrary kubectl command and return its output.
#
# Usage:
#   tools/agent/kubectl_proxy.sh get pods -n bow-test
#   tools/agent/kubectl_proxy.sh kubectl get pods -n bow-test   # leading 'kubectl' optional
#
# Arguments are passed to kubectl untouched. They are taken as argv, never as a
# string handed to a shell, so no argument can start a second command.
#
# The only things rejected are invocations that would never return: this runs
# non-interactively and captures output, so a streaming or interactive command
# would hang until something kills it instead of producing a result.
#
# Environment:
#   KUBECTL_TIMEOUT   seconds before the command is killed (default: 120)
set -euo pipefail

KUBECTL_TIMEOUT="${KUBECTL_TIMEOUT:-120}"

# ── Helpers ─────────────────────────────────────────────────────────────────

usage() {
  echo "Usage: $0 <kubectl args...>"
  echo "Example: $0 get pods -n bow-test"
  exit 1
}

reject() {
  echo "kubectl_proxy: $1" >&2
  exit 64
}

# Locate the subcommand so the checks below can be scoped to it.
#
# kubectl accepts global flags before the subcommand, and several take a
# separate value ("-n bow-test get pods"), so the first bare word is not
# necessarily the subcommand - the value has to be skipped first.
find_subcommand() {
  local a skip_next=0
  for a in "$@"; do
    if [ "$skip_next" -eq 1 ]; then skip_next=0; continue; fi
    case "$a" in
      --*=*) continue ;;
      -n|--namespace|--context|--cluster|--kubeconfig|--user|--as|--as-group|\
      -s|--server|--token|--request-timeout|--cache-dir|--tls-server-name|\
      --certificate-authority|--client-certificate|--client-key|\
      --password|--username|-v|--v)
        skip_next=1; continue ;;
      -*) continue ;;
      *) printf '%s' "$a"; return ;;
    esac
  done
}

# Reject the invocations that cannot return a captured result.
#
# The flag checks are deliberately scoped to a subcommand rather than applied
# across the whole command line: -f means --follow for logs but --filename for
# apply/create/delete/patch, so a blanket ban would break ordinary manifest work.
check_supported() {
  local subcommand="$1"; shift
  local a

  case "$subcommand" in
    port-forward)
      reject "port-forward holds the connection open and never returns, so it cannot report a result here; start it from a shell instead" ;;
    proxy|attach|watch)
      reject "'$subcommand' runs until interrupted, so it cannot return output here" ;;
    edit)
      reject "edit opens an interactive editor; use 'patch' or 'apply' instead" ;;
  esac

  for a in "$@"; do
    case "$subcommand:$a" in
      logs:-f|logs:--follow|logs:--follow=true)
        reject "logs --follow streams until interrupted; use '--tail=N' for a snapshot" ;;
      get:-w|get:--watch|get:--watch-only|get:--watch=true)
        reject "get --watch streams until interrupted; drop the flag to get the current state" ;;
      exec:-i|exec:-t|exec:-it|exec:-ti|exec:--stdin|exec:--tty|exec:--stdin=true|exec:--tty=true)
        reject "exec cannot allocate a TTY here; drop -i/-t and pass the command directly" ;;
    esac
  done
}

# ── Main ────────────────────────────────────────────────────────────────────

case "${1:-}" in
  ""|--help|-h) usage ;;
esac

# Accept both "get pods" and "kubectl get pods".
if [ "$1" = "kubectl" ]; then
  shift
  [ $# -gt 0 ] || usage
fi

SUBCOMMAND="$(find_subcommand "$@")"
[ -n "$SUBCOMMAND" ] || reject "no kubectl subcommand found in: $*"

check_supported "$SUBCOMMAND" "$@"

# Output is passed through untouched - no banner - so that -o json/yaml stays
# machine readable. kubectl's exit code is the script's exit code.
#
# The timeout is a backstop for anything that blocks despite the checks above.
# GNU coreutils installs it as 'timeout', Homebrew as 'gtimeout'; when neither
# is present the command still runs, just without the guard.
if command -v timeout >/dev/null 2>&1; then
  exec timeout "$KUBECTL_TIMEOUT" kubectl "$@"
elif command -v gtimeout >/dev/null 2>&1; then
  exec gtimeout "$KUBECTL_TIMEOUT" kubectl "$@"
else
  exec kubectl "$@"
fi

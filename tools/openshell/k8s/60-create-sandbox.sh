#!/usr/bin/env bash
set -euo pipefail

# Requires: source 01-exports.sh (KIND_KUBECONFIG, etc.)
#
# Providers:
#   bow-provider -> type claude-code, injects CLAUDE_CODE_OAUTH_TOKEN placeholder (generic type does NOT inject)
#   github       -> injects GITHUB_TOKEN for git (github.com endpoint is L7/rest so it swaps)
# Sandbox service account (openshell-sandbox) is wired via values.yaml.
# Policy is passed with --policy; image source with --from.
# The bagofwords image runs clone.sh (needs GIT_REPO/GIT_BRANCH) before the agent.
#
# The bash -c body (heredocs, no line-continuations) does three things before claude:
#   1. clone the repo
#   2. write $HOME/.claude.json so the workspace is pre-trusted (no interactive trust dialog)
#   3. write the MCP config to a file (avoids nested-quote hell) and run claude -p

# To use the local registry instead of Docker Hub, swap --from to:
#   --from localhost:32000/bagofwords:dev

openshell sandbox create \
  --name bow-test-2 \
  --no-keep \
  --provider bow-provider \
  --provider github \
  --from localhost:32000/bagofwords:dev \
  --env UV_LINK_MODE=copy \
  --env NUXT_TELEMETRY_DISABLED=1 \
  --env GIT_BRANCH=feature/secure-data-tunnel \
  --env GIT_REPO=https://github.com/Dimss/bagofwords.git \
  --policy policy.yaml \
  -- bash -c '
set -e
. clone.sh
cat > "$HOME/.claude.json" <<JSON
{"projects":{"/sandbox/app":{"hasTrustDialogAccepted":true}}}
JSON
cat > /tmp/mcp.json <<JSON
{"mcpServers":{"bow-k8s":{"type":"sse","url":"http://mcp-server.openshell.svc:8080/sse"}}}
JSON
claude -p "list all pods in all namespaces" --mcp-config /tmp/mcp.json --allowedTools "mcp__bow-k8s__*"
'

openshell sandbox create \
  --name bow-test-2 \
  --no-keep \
  --provider bow-provider \
  --provider github \
  --from localhost:32000/bagofwords:dev \
  --env UV_LINK_MODE=copy \
  --env NUXT_TELEMETRY_DISABLED=1 \
  --env GIT_BRANCH=feature/secure-data-tunnel \
  --env GIT_REPO=https://github.com/Dimss/bagofwords.git \
  --policy policy.yaml \
  -- bash -c '. clone.sh && claude -p "list all pods in all namespaces"'

# --- Current command: baked-config image (dev-mcp) -------------------------------
# Trust, the bow-k8s MCP server, and the mcp__bow-k8s allow entry are baked into
# the image (/sandbox/.claude.json + /sandbox/.claude/settings.json), so no
# --mcp-config / --allowedTools / trust heredoc is needed here.
# NOTE: rebuilds must use a fresh tag (the node caches :dev with IfNotPresent).
openshell sandbox create \
  --name bow-test-2 \
  --no-keep \
  --provider bow-provider \
  --provider github \
  --from localhost:32000/bagofwords:dev-mcp \
  --env UV_LINK_MODE=copy \
  --env NUXT_TELEMETRY_DISABLED=1 \
  --env GIT_BRANCH=feature/secure-data-tunnel \
  --env GIT_REPO=https://github.com/Dimss/bagofwords.git \
  --policy policy.yaml \
  -- bash -c '
set -e
. clone.sh
claude -p "run Verification of Phase 2" --dangerously-skip-permissions --append-system-prompt "$(cat docs/design/secure-data-tunnel-implementation-system-prompt.md)"
'





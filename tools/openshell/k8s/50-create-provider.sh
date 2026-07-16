source 01-exports.sh

# Must be type claude-code: the generic type stores the credential but does NOT
# inject it as an env var placeholder into the sandbox. claude-code injects
# CLAUDE_CODE_OAUTH_TOKEN=openshell:resolve:env:... which the L7 proxy swaps.
openshell provider create --name "${BOW_OPENSHELL_PROVIDER_NAME}" \
 --type claude-code \
 --credential CLAUDE_CODE_OAUTH_TOKEN="${CLAUDE_CODE_OAUTH_TOKEN}"

openshell provider create \
 --name github \
 --type github \
 --credential GITHUB_TOKEN="$GITHUB_TOKEN"
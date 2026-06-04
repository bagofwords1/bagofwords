#!/usr/bin/env bash
# Configure Anthropic provider + Haiku model from an API key passed as $1.
# Usage: ./setup_llm.sh sk-ant-...
set -euo pipefail
KEY="${1:?pass the Anthropic API key as arg 1}"
B=http://localhost:8000
TOKEN=$(cat /tmp/tok); ORG_ID=$(cat /tmp/org)
H=(-H "Authorization: Bearer $TOKEN" -H "X-Organization-Id: $ORG_ID" -H "Content-Type: application/json")

echo "=== create anthropic provider + haiku model ==="
RESP=$(curl -s -X POST "${H[@]}" "$B/api/llm/providers" -d "{
  \"name\": \"Anthropic\",
  \"provider_type\": \"anthropic\",
  \"credentials\": {\"api_key\": \"$KEY\"},
  \"models\": [
    {\"name\": \"Claude Haiku 4.5\", \"model_id\": \"claude-haiku-4-5\", \"is_default\": true, \"is_small_default\": true}
  ]
}")
echo "$RESP" | python3 -c "import sys,json;d=json.load(sys.stdin);print('provider',d.get('id'),'models',[(m['model_id'],m['id']) for m in d.get('models',[])])"

echo "=== enabled models ==="
curl -s "${H[@]}" "$B/api/llm/models?is_enabled=true" | python3 -c "import sys,json;[print(m['model_id'],'default=',m.get('is_default'),'small=',m.get('is_small_default'),'enabled=',m.get('is_enabled')) for m in json.load(sys.stdin)]"

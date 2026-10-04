#!/usr/bin/env bash
# Seed a fresh sandbox for the file-access loop, API only:
#   admin (first user) + three invited members (alice, bob, carol),
#   a personal bow_ API key for each, and optionally an Anthropic provider.
# Usage (from backend/, backend running on :8000 against db/app.db):
#   bash ../docs/feedback-loops/file-access-scope/seed.sh <state-dir>
set -eu
S=${1:?state-dir}; mkdir -p "$S"
BASE=${BASE:-http://localhost:8000}
DB=${DB:-db/app.db}
PW='Password123!'

register() { curl -s -o /dev/null -w "register $1 %{http_code}\n" -X POST $BASE/api/auth/register \
  -H 'Content-Type: application/json' -d "{\"name\":\"$1\",\"email\":\"$1@example.com\",\"password\":\"$PW\"${2:+,\"invite_token\":\"$2\"}}"; }
login() { curl -s -X POST $BASE/api/auth/jwt/login -d "username=$1@example.com&password=$PW" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])"; }
api_key() { curl -s -X POST $BASE/api/api_keys -H "Authorization: Bearer $1" -H "X-Organization-Id: $ORG" \
  -H 'Content-Type: application/json' -d '{"name":"automation"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['key'])"; }

register admin
ADMIN_JWT=$(login admin)
ORG=$(curl -s $BASE/api/organizations -H "Authorization: Bearer $ADMIN_JWT" | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['id'])")
echo "$ORG" > "$S/org"
api_key "$ADMIN_JWT" > "$S/key_admin"

for u in alice bob carol; do
  curl -s -o /dev/null -X POST $BASE/api/organizations/$ORG/members -H "Authorization: Bearer $ADMIN_JWT" \
    -H "X-Organization-Id: $ORG" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$u@example.com\",\"role\":\"member\",\"organization_id\":\"$ORG\"}"
  # No SMTP in the sandbox: read the pending invite token instead of an email.
  TOK=$(python3 -c "import sqlite3,sys;r=sqlite3.connect(sys.argv[1]).execute(\"SELECT invite_token FROM memberships WHERE email=? AND user_id IS NULL ORDER BY created_at DESC LIMIT 1\",(sys.argv[2],)).fetchone();print(r[0] if r else '')" "$DB" "$u@example.com")
  register $u "$TOK"
  api_key "$(login $u)" > "$S/key_$u"
done

if [ -n "${ANTHROPIC_KEY:-}" ]; then
  python3 - "$ANTHROPIC_KEY" > "$S/prov.json" <<'PY'
import json, sys
print(json.dumps({"name": "Anthropic-Haiku", "provider_type": "anthropic", "credentials": {"api_key": sys.argv[1]},
  "models": [{"name": "Claude 4.5 Haiku", "model_id": "claude-haiku-4-5-20251001", "is_enabled": True,
              "is_default": True, "is_small_default": True, "supports_vision": True}]}))
PY
  curl -s -o /dev/null -w "llm provider %{http_code}\n" -X POST $BASE/api/llm/providers \
    -H "Authorization: Bearer $(cat $S/key_admin)" -H "X-Organization-Id: $ORG" -H 'Content-Type: application/json' -d @"$S/prov.json"
  rm -f "$S/prov.json"
fi
echo "seeded org $ORG into $S"

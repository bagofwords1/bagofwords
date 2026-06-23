# Sandbox Feedback Loop — Integrations & Per-User Tool Connections

Runnable loop for building/validating the **Integrations** feature
(`docs/design/integrations.md`): users connect their own tools and `@mention` them
in any conversation, independent of Agents.

This doc is the reproducible dev harness used in a fresh cloud sandbox.

---

## Environment setup (fresh sandbox)

App targets **Python 3.12** and **Node 22**.

```bash
# Backend deps
cd backend
uv sync --extra dev

# Env (sqlite + dev config + dummy keys; encryption key must be a valid Fernet key)
export BOW_DATABASE_URL="sqlite:///db/app.db"
export BOW_CONFIG_PATH=/home/user/bagofwords/bow-config.dev.yaml
export BOW_ENCRYPTION_KEY="$(uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export ANTHROPIC_API_KEY="sk-ant-dummy-dev-key-not-used"   # not invoked by these flows
mkdir -p db
uv run alembic upgrade head

# Run server
uv run python main.py        # http://localhost:8000

# Frontend
cd ../frontend
yarn install
yarn dev                     # http://localhost:3000
```

`bow-config.dev.yaml` has `allow_uninvited_signups: true`, so the first user can
register over HTTP.

---

## Seed harness (admin + org + token)

```bash
# Register first admin, log in, create org
curl -s -X POST http://localhost:8000/api/auth/register -H 'Content-Type: application/json' \
  -d '{"name":"Admin","email":"admin@bow.dev","password":"password123"}'

TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/jwt/login \
  --data-urlencode 'username=admin@bow.dev' --data-urlencode 'password=password123' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

ORG=$(curl -s -X POST http://localhost:8000/api/organizations \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Dev Org"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
```

All authenticated calls take `Authorization: Bearer $TOKEN` and
`X-Organization-Id: $ORG`.

`scripts/seed_integrations.py` seeds a full integration scenario directly against
the DB (org + admin + a user-required integration connection + its ConnectionTools +
a user credential + UserConnectionTool overlays) for fast iteration without OAuth:

```bash
cd backend && uv run python scripts/seed_integrations.py
```

---

## Validating records (SQLite)

After any flow, validate the persisted rows:

```bash
cd backend
sqlite3 db/app.db "SELECT type, name, auth_policy, allowed_user_auth_modes FROM connections;"
sqlite3 db/app.db "SELECT name, is_enabled FROM connection_tools;"
sqlite3 db/app.db "SELECT connection_id, user_id, auth_mode FROM user_connection_credentials;"
sqlite3 db/app.db "SELECT connection_id, user_id, tool_name, is_accessible FROM user_connection_tools;"
sqlite3 db/app.db "SELECT type, object_id, mention_content FROM mentions ORDER BY created_at DESC LIMIT 10;"
```

---

## Loops

### Loop A — Registry / catalog (API)
List the integration-surface connectors and the org's connected integrations:

```bash
curl -s http://localhost:8000/api/data_sources/registry \
  -H "Authorization: Bearer $TOKEN" -H "X-Organization-Id: $ORG" | python3 -m json.tool | head -60
curl -s "http://localhost:8000/api/integrations" \
  -H "Authorization: Bearer $TOKEN" -H "X-Organization-Id: $ORG" | python3 -m json.tool | head -40
```

### Loop B — Mentions (API)
Confirm a user's connected integrations show up in the `@` autocomplete without any
agent selected, and that mentioning one persists a `CONNECTION` mention:

```bash
curl -s "http://localhost:8000/mentions/available?categories=integrations" \
  -H "Authorization: Bearer $TOKEN" -H "X-Organization-Id: $ORG" | python3 -m json.tool
```

### Loop C — Frontend (Playwright screenshots)
```bash
cd frontend && node scripts/pw-shot.js http://localhost:3000/integrations \
  /tmp/.../scratchpad/shots/integrations.png
```

---

## Status / what each phase proves
- **Phase 1:** `mentions/available?categories=integrations` returns the user's
  connected tools with **no** `data_source_ids`; mentioning persists a `CONNECTION`
  mention; runtime resolves the client by current user.
- **Phase 2:** `/integrations` page renders the catalog; Connect writes per-user
  creds; per-tool toggles write `UserConnectionTool`.
- Later phases extend auth (provider-app/DCR), files (attach-by-link), and connectors.

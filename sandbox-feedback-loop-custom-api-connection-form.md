# Plan + Sandbox Feedback Loop — custom_api connection edit form

Fixes two bugs reported from the **Edit connection** modal for a `custom_api`
connection:

1. **`[object Object]` config fields.** The generic connection form rendered the
   `headers` (object) and `endpoints` (array) config fields into plain text
   inputs, so they showed literally `[object Object]` and were uneditable.
2. **Green "Connected … (HTTP 404)".** The connection test reported *success*
   (green) for any HTTP response, including 404/401/5xx — i.e. a broken/unreachable
   endpoint looked connected.

This doc is both the implementation plan and the runnable UI verification loop
(Playwright). **Status: implemented and verified** — before/after screenshots
captured against the running app (see `frontend/tests-output/`).

---

## Root cause

| Bug | Site | What it did |
|---|---|---|
| `[object Object]` | `frontend/components/datasources/ConnectForm.vue:36` | Object/array config fields fell through to the final `v-else` `<input type="text" v-model="config[field]">`. Binding an object to a text input stringifies it to `[object Object]`. |
| Green 404 | `backend/app/data_sources/clients/custom_api_client.py:171` | `test_connection()` did `client.head(base_url)` and returned `{"success": True, …}` for **any** status code. |

The `custom_api` config schema (`GET /api/data_sources/custom_api/fields`):

```
base_url  -> type=string  ui:type=string
headers   -> type=object  ui:type=None
endpoints -> type=array   ui:type=json
```

Neither `headers` nor `endpoints` matched any existing branch (string / number /
boolean / textarea / password / keyvalue), so both hit the `[object Object]`
fallback.

---

## Fix

**Frontend (`ConnectForm.vue`).** Added a JSON-editor branch for object/array
config fields (anything `type: object|array` that isn't a `keyvalue` field):
a monospace `<textarea>` bound to a string representation (`jsonTextMap`), parsed
back into `formData.config` on every edit with an inline "Invalid JSON" hint when
it doesn't parse. `formData.config` stays the submitted source of truth. Object/
array defaults are seeded in `initFormDefaults`, and the editors are initialized
alongside the keyvalue editors in `initJsonFields()`.

**Backend (`custom_api_client.py`).** `test_connection()` now only reports success
for a reachable, OK response: `status < 400`, plus `405` (HEAD not allowed on the
base path but the host is reachable). Error statuses return
`{"success": False, "message": "Reached … but it returned HTTP {status} — check
the base URL, endpoint path, and credentials."}`.

---

## Environment setup (fresh sandbox)

Backend (Python 3.12) and frontend (Nuxt) both run locally.

```bash
# Backend
cd backend
python3.12 -m venv /tmp/venv312 && VIRTUAL_ENV=/tmp/venv312 uv sync --frozen --extra dev
export ENVIRONMENT=development BOW_DATABASE_URL="sqlite:///db/app.db"
export BOW_ENCRYPTION_KEY=$(.venv/bin/python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
mkdir -p db uploads/files && .venv/bin/python -m alembic upgrade head
.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1 &

# Frontend — the sandbox proxy allows registry.npmjs.org directly but proxies
# (and aborts) registry.yarnpkg.com, so install via the npm registry:
cd ../frontend
NODE_EXTRA_CA_CERTS=/root/.ccr/ca-bundle.crt npm install --no-audit --no-fund --legacy-peer-deps
NODE_EXTRA_CA_CERTS=/root/.ccr/ca-bundle.crt yarn dev &   # http://127.0.0.1:3000
```

---

## Loop A — backend test_connection (HTTP status → success)

Unit test (`backend/tests/unit/test_custom_api_client.py`) mocks `httpx` and
asserts the status→success mapping:

```bash
cd backend && export BOW_DATABASE_URL="sqlite:///db/app.db"
.venv/bin/python -m pytest tests/unit/test_custom_api_client.py -q
```

**Observed (PASS, 10 cases):** 200/204/302/405 → success; 400/401/403/404/500 → failure.

Live proof through the real API: creating the connection with
`base_url=https://api.anthropic.com/v1` (which 404s on HEAD) now returns
**HTTP 400** *"Reached … but it returned HTTP 404 — check the base URL …"*
instead of a green success.

---

## Loop B — UI before/after (Playwright)

Rendered the real `ConnectForm` (edit mode, `custom_api`) against the running app
with a seeded connection whose `headers` and `endpoints` are object/array, auth
injected via the `auth.token` cookie + `bow.selectedOrganizationId` localStorage.

| Before (fix reverted) | After (fix applied) |
|---|---|
| `frontend/tests-output/connect-form-before.png` | `frontend/tests-output/connect-form-after.png` |
| Custom Headers = `[object Object]`, Endpoints = `[object Object]` | Custom Headers = real JSON `{ "anthropic-version": "2023-06-01", … }`, Endpoints = JSON array, both editable, **0** occurrences of `[object Object]` |

---

## Files

| File | Change |
|---|---|
| `frontend/components/datasources/ConnectForm.vue` | JSON-editor branch + `jsonTextMap`/`jsonSync`/`initJsonFields` + object/array defaults |
| `backend/app/data_sources/clients/custom_api_client.py` | `test_connection` success only for reachable/OK status |
| `backend/tests/unit/test_custom_api_client.py` | **new** — status→success mapping test |

Both fixes are scoped to the connection-edit experience and unrelated to the MCP
gateway in the same branch; they ride along on the consolidated PR.

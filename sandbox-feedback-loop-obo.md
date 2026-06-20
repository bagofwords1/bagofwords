# Sandbox Feedback Loop — Entra ID SSO + Fabric/PowerBI OBO (end-to-end)

Goal: thoroughly validate the Microsoft integration on the current branch
(`claude/kind-gates-0s7bqk`) end-to-end:

1. **Entra ID login is enabled** (`configs/bow-config.dev.yaml`, `oidc_providers: entra`)
   and users can sign in with their Microsoft creds.
2. An admin **creates a Fabric/PowerBI connection with a service account**
   (service principal: tenant + client_id + client_secret) and
   `auth_policy=user_required`, `allowed_user_auth_modes=["oauth"]`.
3. A user **signs in with their own Entra creds**, gets an **OBO** delegated
   token, **sees the tables they're allowed to see**, and **queries run through
   their own identity** (row/table ACLs enforced by Fabric, not by us).
4. An **admin can switch query identity** between **service account** (`system`)
   and **as the user** (`self`/`user`) per connection.
5. **Agent / data-source permissions** behave correctly (visibility + usability).

> ⚠️ **Secrets**: all credentials below are passed via **env vars only — never
> commit them**. The values the user provided live only in the running shell
> session, not in any file.

---

## 0. What's already in the repo (reuse, don't reinvent)

| Asset | Path | Use |
|---|---|---|
| Entra-enabled dev config | `configs/bow-config.dev.yaml` (and `…dev.entra.yaml`) | `oidc_providers: entra enabled: true`, app `client_id`/tenant already filled in |
| Live OBO integration tests | `backend/tests/integrations/test_oauth_delegated.py` | client-creds, ROPC login, OBO exchange, Fabric SQL, token refresh |
| App-logic overlay repro | `backend/tests/e2e/test_fabric_second_admin_overlay_repro.py` | "second admin sees no tables" regression |
| OAuth flow e2e | `backend/tests/e2e/test_connection_oauth_flow.py` | authorize/callback/credentials |
| OIDC group sync e2e | `backend/tests/e2e/test_oidc_group_sync.py` | Entra group → role mapping |
| Existing bug-repro doc | `sandbox-feedback-loop.md` | the overlay/reload scoping bug + fix |

### Architecture cheat-sheet (file:line)

- **Connection auth fields**: `auth_policy` (`system_only`|`user_required`),
  `allowed_user_auth_modes` (`["oauth"]`) — `backend/app/models/connection.py:32-33`.
- **OBO connection types**: `{powerbi, ms_fabric, sharepoint, onedrive}` —
  `connection_oauth_service.py:279`.
- **OBO scopes** — `connection_oauth_service.py:285-294`:
  - `ms_fabric` → `https://database.windows.net/user_impersonation offline_access`
    (Azure SQL token; **not** the Fabric API scope — the SQL endpoint rejects it).
  - `powerbi` → `https://analysis.windows.net/powerbi/api/.default offline_access`.
- **OBO exchange**: `exchange_obo_token()` `connection_oauth_service.py:297-356`.
- **Auto-provision on login**: `auto_provision_connection_credentials()`
  `connection_oauth_service.py:363+`, triggered from `auth_providers.py` on Entra login.
- **Effective auth (`system`|`user`|`none`)**: `connection_identity.build_token_identity_status()`
  `:75-128`; `data_source_service._resolve_effective_auth()` `:2205-2227`.
- **Admin identity toggle**: `PATCH /connections/{id}/query-identity`
  `routes/connection.py:479-540` (`self` | `service_account`; admin/owner only;
  rejects non-`user_required` connections).
- **Display scoping / overlay**: `get_data_source_schema_paginated()`,
  `get_user_data_source_schema()`, `_refresh_shared_user_overlay()` in
  `data_source_service.py`; `UserDataSourceTable` overlay model.
- **Fabric SQL client**: `data_sources/clients/ms_fabric_client.py` — prefers
  delegated `access_token`, falls back to service principal. Needs **pyodbc +
  ODBC Driver 18 for SQL Server**.
- **OAuth routes**: `routes/connection_oauth.py` — `GET …/oauth/authorize`,
  `GET …/oauth/callback` (signed-state JWT + PKCE).
- **Permissions**: `core/permission_resolver.py`; DS visibility/usability filters
  `data_source_service.py:~1714-1800`.

---

## 1. Sandbox environment — how it's spun up

Python **3.12** (app uses 3.12 syntax). Node 22 / yarn present. SQLite for speed.

```bash
# ---- backend ----
cd /home/user/bagofwords/backend
python3.12 -m venv /tmp/venv312
/tmp/venv312/bin/pip install -q --upgrade pip

# Full reqs fail in a bare sandbox: psycopg2 needs pg_config, pyodbc/pymssql need
# ODBC/FreeTDS headers. We run on SQLite, so drop the native DB clients:
grep -ivE '^psycopg2|^pyspark|^thrift|^pyodbc|^grpcio-tools|^confluent-kafka|^snowflake|^cx[-_]Oracle|^oracledb|^pymssql|^sqlalchemy-bigquery|^google-cloud' \
  requirements_versioned.txt > /tmp/reqs_lite.txt
/tmp/venv312/bin/pip install -q -r /tmp/reqs_lite.txt   # server boots; no live Fabric SQL

# ---- required env (secrets via env only) ----
export BOW_CONFIG_PATH=/home/user/bagofwords/configs/bow-config.dev.yaml
export BOW_DATABASE_URL="sqlite:///db/app.db"
export BOW_ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
export BOW_ENTRA_CLIENT_SECRET='***'        # app secret value
# (license / smtp keys optional for local)

# ---- migrate + run ----
mkdir -p db
/tmp/venv312/bin/alembic upgrade head
/tmp/venv312/bin/python main.py                 # backend :8000

# ---- frontend ----
cd /home/user/bagofwords/frontend
yarn install
yarn dev                                          # :3000  (proxies /api → :8000)
```

Live-OBO env vars (for `tests/integrations/test_oauth_delegated.py`):

```bash
export BOW_ENTRA_TENANT_ID=3871cb7e-3f16-4b81-84c5-1a5e185509f9
export BOW_ENTRA_CLIENT_ID=a9010cd3-08ff-451b-b041-5007a94ba677
export BOW_ENTRA_CLIENT_SECRET='***'
export BOW_OAUTH_TEST_DEMO1_EMAIL=demo1@bow14.onmicrosoft.com
export BOW_OAUTH_TEST_DEMO1_PASSWORD='***'
export BOW_OAUTH_TEST_DEMO2_EMAIL=demo2@bow14.onmicrosoft.com
export BOW_OAUTH_TEST_DEMO2_PASSWORD='***'
export BOW_FABRIC_SERVER=p3fxcoawh6auxbgfdjpbqvij7e-simszbl3ii5uthh3ulrmrqf4sq.datawarehouse.fabric.microsoft.com
export BOW_FABRIC_DATABASE=demo_db
```

### ⚠️ Known sandbox blockers (decide before relying on a test)

1. **Browser-based Entra SSO needs a public redirect URI.** Real interactive
   login redirects to `…/api/auth/entra/callback` and `…/connections/oauth/callback`.
   Those must be **registered redirect URIs** on the Entra app, reachable from the
   user's browser. `base_url: http://localhost:3000` only works if the sandbox is
   tunnelled/port-forwarded to a URL that is also registered in Azure. → For UI
   Playwright tests against real Entra, either (a) register the tunnel URL, or
   (b) drive the **non-interactive ROPC** path in the backend and inject the
   resulting tokens, and use Playwright only for the in-app screens.
2. **Fabric SQL queries need ODBC Driver 18 + pyodbc.** Not installed in a bare
   sandbox. Without it, `MSFabricClient.connect()`/query tests **skip**. Install
   `msodbcsql18` (Microsoft apt repo) to exercise the real SQL path.
3. **OBO is likely EE/license-gated** in places — confirm whether
   `auth_policy=user_required` + identity toggle require `BOW_LICENSE_KEY`.
4. **Scope mismatch to verify**: the integration test uses Fabric OBO scope
   `https://api.fabric.microsoft.com/.default`, but production code requests
   `https://database.windows.net/user_impersonation` (`connection_oauth_service.py:290`).
   The **production** scope is what the SQL endpoint accepts — make sure the live
   verification exercises the production scope, not just the test's scope.

---

## 2. Verification matrix

Legend: **BE** = backend/pytest · **API** = HTTP/curl against running server ·
**UI** = Playwright · **AZ** = needs live Azure · **ODBC** = needs ODBC driver.

### A. Entra auth registration & config

| # | Check | How | Tags |
|---|---|---|---|
| A1 | `entra` provider detected from config | `pytest …/test_oauth_delegated.py -k TestIsEntraProvider` | BE |
| A2 | Sign-in page renders the **"Sign in with Entra/Microsoft"** button | UI: load `/users/sign-in`, assert provider button visible | UI |
| A3 | App can get client-credentials token for Graph/Fabric/PowerBI | `pytest -k TestClientCredentials` | BE, AZ |
| A4 | `/api/auth/entra/login` returns a Microsoft authorize URL w/ correct tenant+client+PKCE | API curl, assert `login.microsoftonline.com/<tenant>` + `code_challenge` | API |
| A5 | OIDC group sync maps Entra groups → roles | `pytest tests/e2e/test_oidc_group_sync.py` | BE |

### B. User authentication (Entra login → app session)

| # | Check | How | Tags |
|---|---|---|---|
| B1 | demo1 & demo2 can obtain Entra login tokens (ROPC) | `pytest -k TestROPCLogin` | BE, AZ |
| B2 | Interactive SSO: demo1 logs into the app via the Entra button → lands authenticated | UI (needs registered redirect) | UI, AZ |
| B3 | On login, `auto_provision_connection_credentials` runs for eligible connections | BE: after login, assert `UserConnectionCredentials` row created for the Fabric connection | BE/API, AZ |
| B4 | New uninvited user blocked unless allowed (`allow_uninvited_signups:false`) | API: login as unknown user → expected gate | API |

### C. Connection creation with a service account (admin)

| # | Check | How | Tags |
|---|---|---|---|
| C1 | Admin creates `ms_fabric` connection with SP creds + `auth_policy=user_required`, `allowed_user_auth_modes=["oauth"]` | UI: New connection wizard → Fabric; **or** API POST `/api/data_sources` | UI/API |
| C2 | Service-principal connection test passes (system creds) | UI "Test connection" / `test_connection()` | UI/API, AZ, ODBC |
| C3 | Canonical catalog populated via SP (`sync_domain_tables_from_connection`) | API: GET schema as admin shows full catalog (sales+finance) | API, AZ, ODBC |
| C4 | Credentials stored **encrypted** (no plaintext secret in DB) | BE: query `connections` row, assert ciphertext | BE |

### D. OBO exchange (user delegated token)

| # | Check | How | Tags |
|---|---|---|---|
| D1 | OBO exchange demo1 & demo2 → Fabric token | `pytest -k "TestOBOExchange or TestOBOServiceFunction"` | BE, AZ |
| D2 | OBO exchange → PowerBI token | same | BE, AZ |
| D3 | `exchange_obo_token()` uses **production** Fabric scope (`database.windows.net/user_impersonation`) and the resulting token is **accepted by the SQL endpoint** | BE+ODBC: exchange via service fn, then `MSFabricClient.connect()` | BE, AZ, ODBC |
| D4 | Token refresh: OBO refresh_token → new access_token | `pytest -k TestTokenRefreshFlow` | BE, AZ |
| D5 | `UserConnectionCredentials.expires_at` set; refresh fires <5min before expiry | BE unit | BE |

### E. Per-user table visibility (the core promise)

| # | Check | How | Tags |
|---|---|---|---|
| E1 | demo1 (AllFabric) sees **sales + finance**; demo2 (MinimalFabric) sees **sales only** | `pytest -k "TestFabricClientDelegated"` | BE, AZ, ODBC |
| E2 | After demo2 signs in, the **Tables Selector** shows only their tables (overlay-scoped) | UI: open data-source tables panel as demo2 | UI, AZ, ODBC |
| E3 | **Second admin / second user sees tables** (the prior bug) — overlay populated on connect + reload | `pytest tests/e2e/test_fabric_second_admin_overlay_repro.py` | BE |
| E4 | "Reload my tables" repopulates the caller's overlay (`_refresh_shared_user_overlay`) | UI button + API; assert overlay rows after | UI/API, AZ, ODBC |
| E5 | Revoking demo2's SELECT upstream → table disappears from overlay (status=revoked) on next sync | BE/manual | BE, AZ, ODBC |

### F. Querying runs through the user's identity

| # | Check | How | Tags |
|---|---|---|---|
| F1 | demo1 query `dbo.finance` returns rows; demo2 query `dbo.finance` is **denied/empty** (Fabric ACL) | `pytest -k "test_demo1_query or test_demo2"` | BE, AZ, ODBC |
| F2 | A chat/report run by demo2 only reaches tables in their overlay; LLM context excludes finance | UI: ask agent a question; inspect tables used | UI, AZ, ODBC |
| F3 | Query path resolves credentials by `effective_auth` (user token, not SP) | BE: assert resolve_credentials returns delegated token when `effective_auth=user` | BE |

### G. Admin identity switching (service account ↔ user)

| # | Check | How | Tags |
|---|---|---|---|
| G1 | `PATCH /connections/{id}/query-identity {service_account}` → `effective_auth=system`, full catalog | API/UI toggle in ConnectionDetailModal | API/UI |
| G2 | Switch back to `self` → `effective_auth=user`, overlay-scoped tables | API/UI | API/UI |
| G3 | Toggle **rejected (400)** for `system_only` connections / non-oauth | API negative test | API |
| G4 | Toggle **rejected (403)** for non-admin/non-owner | API negative test | API |
| G5 | UI toggle reflects state: "Me" connected shows Reload/Disconnect; "Service Account" read-only | UI | UI |

### H. Connection OAuth flow (manual "Connect" per connection)

| # | Check | How | Tags |
|---|---|---|---|
| H1 | `GET /connections/{id}/oauth/authorize` → Microsoft URL with PKCE + signed state | `pytest -k TestAuthorizeEndpoint` / API | BE/API |
| H2 | Callback exchanges code, stores creds, **tests token, rolls back on failure** | `pytest tests/e2e/test_connection_oauth_flow.py` | BE |
| H3 | UI: "Connect" in ConnectionDetailModal → Microsoft → back to `/data?oauth=success` → tables load | UI | UI, AZ |
| H4 | State tamper / expired state rejected | BE negative | BE |

### I. Agent / data-source permissions

| # | Check | How | Tags |
|---|---|---|---|
| I1 | Public vs private data source visibility (`is_public`) | `pytest tests/e2e/rbac/test_rbac_data_sources.py` | BE |
| I2 | User with no token + not admin → DS **not usable** (`effective_auth=none`, skipped) | BE: `filter_user_usable_data_sources` | BE |
| I3 | Setting per-agent / per-DS grants restricts which users/agents can query | UI (Knowledge Explorer / access) + BE | UI/BE |
| I4 | Agent run as demo2 cannot read finance even if instructed (overlay + Fabric ACL) | UI | UI, AZ, ODBC |

### J. Regression / negative

| # | Check | How | Tags |
|---|---|---|---|
| J1 | Full delegated-OAuth unit suite | `pytest tests/unit/test_connection_oauth.py` | BE |
| J2 | Expired/invalid delegated token → graceful "reconnect required", not 500 | BE/UI | BE/UI |
| J3 | Mixed: admin on service account + user on self, same connection, concurrent — no overlay bleed | BE/manual | BE |

---

## 3. Suggested run order (fast → slow)

```bash
cd /home/user/bagofwords/backend
export BOW_DATABASE_URL="sqlite:///db/app.db"

# 1) No-Azure, no-ODBC (logic + config) — fastest signal
/tmp/venv312/bin/python -m pytest \
  tests/integrations/test_oauth_delegated.py -k "TestIsEntraProvider or TestGetOAuthParams" \
  tests/e2e/test_fabric_second_admin_overlay_repro.py \
  tests/unit/test_connection_oauth.py -v

# 2) Live Azure, no ODBC (tokens only) — needs BOW_ENTRA_* + demo creds + ROPC enabled
/tmp/venv312/bin/python -m pytest tests/integrations/test_oauth_delegated.py \
  -k "TestClientCredentials or TestROPCLogin or TestOBOExchange or TestOBOServiceFunction or TestTokenRefreshFlow" -v

# 3) Live Azure + ODBC (real Fabric SQL, per-user ACLs) — needs ODBC Driver 18
/tmp/venv312/bin/python -m pytest tests/integrations/test_oauth_delegated.py \
  -k "TestFabricClientDelegated" -v

# 4) OAuth flow + group sync e2e
/tmp/venv312/bin/python -m pytest \
  tests/e2e/test_connection_oauth_flow.py tests/e2e/test_oidc_group_sync.py -v
```

UI (Playwright) — once backend `:8000` + frontend `:3000` are up:
- A2 sign-in button, G5/H3 ConnectionDetailModal toggle + connect, E2/E4 tables
  selector, F2/I4 agent runs. (Real-Entra UI steps need a registered redirect URI;
  otherwise seed an app session + inject ROPC/OBO tokens and drive only in-app screens.)

---

## 4. Status log (this sandbox run, 2026-06-20)

| Phase | Status | Notes |
|---|---|---|
| Backend deps installed | ✅ | `/tmp/venv312`, lite reqs (no psycopg2/pyodbc) |
| Frontend deps installed | ✅ | `yarn install` clean |
| `alembic upgrade head` | ✅ | SQLite `backend/db/app.db` |
| Backend `:8000` up | ✅ | uvicorn, Community license |
| Frontend `:3000` up | ✅ | Nuxt dev, proxies `/api` → :8000 |
| A4 authorize URL | ✅ | `GET /api/auth/entra/authorize` → correct tenant/client/PKCE/`access_as_user`, `redirect_uri=http://localhost:3000/api/auth/entra/callback` |
| Tier-1 (logic) tests | ⚠️ | 13 passed, **2 failed** (stale Fabric scope assertion — see Findings F-1) |
| Tier-2 (live tokens) | ✅ | **10 passed, 1 skipped** — client-creds (Graph/Fabric/PowerBI), demo1+demo2 ROPC login, OBO→Fabric (both users), OBO→PowerBI, `exchange_obo_token`, token refresh. ⚠️ used the test's `api.fabric.microsoft.com` scope, not the production `database.windows.net` scope (F-1) |
| Tier-3 (Fabric SQL) | ⛔ | blocked — no ODBC Driver 18 in sandbox |
| UI Playwright | ☐ | needs registered redirect URI or token injection |

Env: `source /tmp/bow_env.sh` (all secrets; not committed). Servers:
backend log `/tmp/bow_backend.log`, frontend log `/tmp/bow_frontend.log`.

## 5. Findings

- **F-1 (test bug, not product):** `get_oauth_params(ms_fabric)` returns scope
  `https://database.windows.net/user_impersonation offline_access` (correct — the
  Fabric Warehouse SQL endpoint needs Azure SQL tokens, see
  `connection_oauth_service.py:287-290`). But two tests still assert the **old**
  `api.fabric.microsoft.com` scope and fail:
  - `tests/unit/test_connection_oauth.py::TestGetOAuthParams::test_ms_fabric:93`
  - `tests/integrations/test_oauth_delegated.py::TestGetOAuthParams::test_fabric_params`
  Also `test_oauth_delegated.py` uses `FABRIC_SCOPE = api.fabric.microsoft.com/.default`
  for its **live** OBO exchanges — so the live OBO tests exercise a scope the SQL
  endpoint would reject, not the production scope. Update tests to the
  `database.windows.net/user_impersonation` scope.
- **F-2 (caveat):** App runs in **Community** license mode here (no `BOW_LICENSE_KEY`).
  Confirm whether `auth_policy=user_required` + query-identity toggle are EE-gated
  before concluding a UI test "passes/fails" for the wrong reason.


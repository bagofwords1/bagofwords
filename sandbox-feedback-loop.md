# Sandbox Feedback Loop — Entra ID / Fabric OBO for Admins

> Working doc for iterating on: **letting admins authenticate with their own
> Entra ID credentials (OBO) instead of silently falling back to the connection's
> service principal.** Nothing is implemented yet — this captures the current
> behavior, the gap, the test loop, and a space for your creds + your design thoughts.

---

## 1. The ask (as I understand it)

- You have an **Entra ID** deployment with **Microsoft Fabric**.
- An admin sets up a **Fabric connection** + an **agent**, and sets the connection
  to **require per-user auth** (so each user authenticates with their own creds → OBO).
- **Problem:** the *admin* doesn't actually go through OBO. When the admin runs the
  agent, the system silently uses the **service principal** (the app-registration
  `client_id` / `client_secret` stored on the connection) — i.e. "admin-level"
  credentials — instead of the admin's *own* delegated identity.
- **Want:** the admin should also be able to "log in with their own creds for OBO"
  and query Fabric as themselves, **not** as the principal.

If I've misread any of this, correct it in §7 before we go further.

---

## 2. Current architecture (code review)

There are **two** ways a per-user delegated token gets created today, plus a
**fallback** that is the source of the problem.

### 2a. Interactive "Connect" — authorization-code + PKCE
`backend/app/routes/connection_oauth.py`
- `GET /connections/{id}/oauth/authorize` → builds the Entra authorize URL, scopes
  per type (Fabric uses `https://database.windows.net/user_impersonation offline_access`),
  signed-JWT `state` binds `connection_id`+`user_id`, PKCE verifier in a cookie.
- `GET /connections/oauth/callback` → exchanges code → stores a
  **`UserConnectionCredentials`** row (`auth_mode="oauth"`), then **live-tests** it and
  rolls back if the token doesn't actually work.
- Gated by `_ensure_oauth_policy`: connection must be `auth_policy="user_required"`
  **and** have `"oauth"` in `allowed_user_auth_modes`.
- **This path works for anyone, including an admin** — but it has to be triggered
  explicitly (the user clicks "Connect"). An admin never gets *forced* into it (see 2c).

### 2b. Automatic OBO at login
`backend/app/services/auth_providers.py` → `_handle_callback` (line ~420) calls
`auto_provision_connection_credentials` when the OIDC provider is Entra
(`_is_entra_provider`, matches `login.microsoftonline.com` / `sts.windows.net`).

`backend/app/services/connection_oauth_service.py`:
- `auto_provision_connection_credentials` — for every connection that is
  `user_required`, type ∈ `{powerbi, ms_fabric, sharepoint, onedrive}`, and has
  `"oauth"` in `allowed_user_auth_modes`, it runs `exchange_obo_token` using the
  user's **login access token** as the OBO assertion and stores a
  `UserConnectionCredentials` row.
- `exchange_obo_token` — `grant_type=jwt-bearer`, `requested_token_use=on_behalf_of`,
  authenticates with the **connection's** `oauth_client_id`/`oauth_client_secret`,
  Fabric scope = `https://database.windows.net/user_impersonation offline_access`.
- **This is best-effort** — wrapped in `try/except` that only logs a warning
  (`auth_providers.py` line ~427). If OBO fails, login still succeeds and **no user
  credential is stored** → the user later relies on the Connect flow (2a) or the
  fallback (2c).

> ⚠️ **OBO prerequisite (likely the real blocker):** OBO only works if the user's
> *login* access token has **`aud` = the connection app's `client_id`** (i.e. the
> login token is for an API exposed by the same app reg that does the exchange).
> The integration test spells this out:
> `backend/tests/integrations/test_oauth_delegated.py:67-69` —
> *"For OBO to work, the login token must have aud=app's own client_id. This
> requires 'Expose an API' with Application ID URI set on the app registration."*
> If your **OIDC login app** and your **Fabric connection app** are different
> registrations (the common setup), the login token's audience won't match and
> `exchange_obo_token` fails → admins fall straight to 2c, users fall to 2a.

### 2c. The fallback that causes the reported behavior
When a `user_required` connection is queried, credentials are resolved in
`backend/app/services/data_source_service.py`:
- `resolve_credentials_for_connection` (multi-conn, the live path) — lines ~1696-1780
- `resolve_credentials` (legacy single-conn) — lines ~1396-1474

Both resolve in this order:
1. data-source-level user creds (`UserDataSourceCredentials`)
2. connection-level user creds (`UserConnectionCredentials`, incl. OAuth refresh via
   `maybe_refresh_oauth_credentials`)
3. **owner/admin fallback → the connection's stored system creds**
   (`connection.decrypt_credentials()`, i.e. the **service principal**) — lines
   ~1761-1775 (multi) / ~1464-1472 (legacy)
4. otherwise `403 "User credentials required"` (regular users → routed to Connect)

> **This is the crux.** If the admin has no `UserConnectionCredentials` row (because
> OBO at login failed/was skipped, and they never clicked "Connect"), step 3 hands
> them the **service principal** and everything "just works" for them — invisibly as
> the principal, not as themselves. Regular users hit step 4 and are forced into OBO.

The same owner/admin → system-creds fallback also appears in the status/preview
surface: `user_data_source_credentials_service.py` (`build_user_status*`,
`effective_auth="system"`, `uses_fallback=True`).

### 2d. How the token reaches Fabric
`backend/app/data_sources/clients/ms_fabric_client.py`:
- `_get_access_token()` — if a delegated `access_token` was passed (the OBO/Connect
  token), it's used directly; **otherwise** it builds a `ClientSecretCredential`
  from `tenant_id`/`client_id`/`client_secret` (the **service principal**) and gets
  an `https://database.windows.net/.default` token.
- Token is pushed into pyodbc via `attrs_before={1256: token_struct}` (SQL access token).
- So "admin via principal" vs "admin via OBO" comes down entirely to **which creds
  dict §2c returns** — a delegated `access_token` vs the SP `client_id/secret`.

### 2e. Auth-policy / mode storage
- `Connection.auth_policy` (`system_only` | `user_required`) and
  `Connection.allowed_user_auth_modes` (JSON list, e.g. `["oauth"]`) —
  `backend/app/models/connection.py`.
- `Connection.get_credentials()` returns `None` for `user_required` (which is why
  the fallback uses `decrypt_credentials()` directly — see the comment at
  `data_source_service.py:1762-1771`).

---

## 3. Gap summary (one paragraph)

The product *can* authenticate an admin via OBO/Connect — the machinery in 2a/2b
already supports it. But two things conspire to make admins use the principal
instead: (1) the **owner/admin fallback** in `resolve_credentials*` returns the
service principal whenever the admin has no personal credential row, and (2) **OBO
auto-provision at login likely fails** for the typical "separate login app vs Fabric
app" registration (audience mismatch), so the admin never gets a personal row in the
first place. To "enable admin OBO" we need to (a) make sure the admin *gets* a
delegated token, and (b) decide when the principal fallback is allowed vs forbidden.

---

## 4. The sandbox feedback loop

The sandbox **can reach** `login.microsoftonline.com` and `graph.microsoft.com`
(verified: both return 200). So we can run **real token-level OBO exchanges** here.

What runs where:

| Check | Needs | Runnable in sandbox? |
|-------|-------|----------------------|
| Login (ROPC/device code) → access token | `httpx`, network to Entra | ✅ yes |
| OBO exchange (`jwt-bearer`) → Fabric SQL token | `httpx`, network to Entra | ✅ yes |
| Decode token `aud`/`scp`/`upn` to confirm identity | `pyjwt` | ✅ yes |
| Actual Fabric SQL query (`SELECT 1`, table list) | `pyodbc` + ODBC Driver 18 + network to `*.datawarehouse.fabric.microsoft.com` | ⚠️ TBD — driver/pyodbc **not installed yet**; need to verify Fabric host reachability |
| Full app flow (login → resolve_credentials → query) | backend deps + DB | ⚠️ heavier; do after token-level loop is green |

**Existing harness to reuse:** `backend/tests/integrations/test_oauth_delegated.py`
already implements ROPC login, device-code login, OBO exchange, and Fabric
connectivity, all driven by env vars (`BOW_ENTRA_*`, `BOW_OAUTH_TEST_DEMO*`,
`BOW_FABRIC_*`). The loop below mostly feeds this.

### Loop steps
1. **You** drop the Entra app-reg details + Fabric server + ≥2 test users into §5
   (or hand me the secrets to export as env vars — never commit them).
2. **I** run a token-level probe per user: login → OBO → decode token → confirm
   `aud`/`upn`/`scp` say "this user, Fabric scope, delegated" (not the SP). This
   immediately tells us whether OBO is even possible with the current app-reg
   (the §2b audience prerequisite).
3. If OBO succeeds, **I** (optionally) run a Fabric `SELECT 1` per user to confirm
   row-level/permission differences (e.g. admin sees all, user2 sees a subset) —
   pending the pyodbc/driver/host check above.
4. We record each run in §6, decide the enablement design in §7, *then* implement.

### Setup commands (when ready)
```bash
cd backend
python3 -m venv venv && source venv/bin/activate
pip install httpx pyjwt   # minimal, for token-level probes
# (full: pip install -r requirements_versioned.txt  + ODBC Driver 18 for Fabric SQL)
```

---

## 5. Inputs — fill these in (DO NOT commit real secrets)

> Prefer handing me secrets to `export` at runtime. If you paste here, scrub before commit.

### Entra app registration(s)
- Tenant ID: `____`
- **Login** app (OIDC sign-in) client_id: `____`
- **Fabric connection** app client_id: `____`  ← same as login app, or different?
- "Expose an API" / Application ID URI set on the connection app? `yes / no / unknown`
- Delegated permission **Azure SQL Database / user_impersonation** granted + admin consent? `yes / no / unknown`
- ROPC enabled (for non-interactive testing)? `yes / no`

### Fabric
- Server host (e.g. `xxxx.datawarehouse.fabric.microsoft.com`): `____`
- Database / warehouse name: `____`
- Schemas to expose (optional): `____`

### Test users (≥2, ideally with *different* Fabric permissions)
| Label | Email | Role (admin?) | Expected Fabric visibility |
|-------|-------|---------------|----------------------------|
| admin | `____` | admin/owner | e.g. all tables |
| user1 | `____` | member | e.g. sales schema only |
| user2 | `____` | member | e.g. none / denied |

---

## 6. Iteration log

| # | Date | What we ran | Result / token `aud`+`upn` | Notes |
|---|------|-------------|----------------------------|-------|
| 0 | 2026-06-08 | Code review + sandbox net check | Entra & Graph reachable (200); pyodbc not installed | baseline |
| 1 | 2026-06-08 | Live ROPC + OBO probe, demo1 & demo2 (app `a901…ba677`, tenant `3871…09f9`) | **Direct delegated tokens ✅; OBO ❌ (AADSTS50013)** | see §6a — this changes the plan |

### 6a. Iteration 1 findings (live, both users)

**✅ Direct per-user delegated tokens work perfectly** (ROPC, but auth-code/"Connect"
gives the identical token). For each user we got, with the user's own `upn` and
`idtyp=user`:
- **Fabric SQL:** `aud = https://database.windows.net`, `scp = user_impersonation`
  — **this is exactly the token `MsFabricClient._get_access_token()` needs** for the
  pyodbc `attrs_before={1256: ...}` SQL login. Per-user, not the SP.
- PowerBI: `aud = analysis.windows.net/powerbi/api`, `scp = Dataset.Read.All Workspace.Read.All`.
- Fabric API: `aud = api.fabric.microsoft.com`.

**❌ OBO fails for both users:**
```
AADSTS50013: Assertion failed signature validation.
[Key was found, but use of the key to verify the signature failed...]
```
**Why:** the *login* token this app gets has **`aud = 00000003-0000-0000-c000-000000000000`
(Microsoft Graph)**, not `aud = <client_id>`. You cannot use a Graph token as an OBO
assertion for your own app — OBO requires the assertion to be issued *for your API*
(`aud = client_id` / `api://client_id`), which needs **"Expose an API"** on the app
reg (this app doesn't have it). So `connection_oauth_service.exchange_obo_token`
(and the auto-provision-at-login path) **cannot work with this registration** — and
this is the *normal* state of affairs unless you deliberately set up Expose-an-API.

### 6b. Conclusion — OBO is the wrong lever; use the delegated "Connect" flow

The thing labeled "OBO" in the ask is really just **"each user uses their own
delegated token."** We do **not** need the OBO grant to achieve that — the
**interactive Connect flow (§2a)** already mints a per-user
`aud=database.windows.net` token via authorization-code+PKCE and stores it as
`UserConnectionCredentials`. It works for **any** user, **including the admin**.

So enabling "admin uses their own creds, not the principal" reduces to **two code
changes** (still not implemented — pending your §7 sign-off):
1. **Let the admin actually go through Connect** for a `user_required` connection
   (today the admin is never prompted because the fallback short-circuits them).
2. **Gate / remove the service-principal fallback** in
   `data_source_service.resolve_credentials_for_connection` (~1761) and
   `resolve_credentials` (~1464) so an admin without a personal token is prompted to
   Connect instead of silently getting the SP. (Likely keep SP available only for
   background **indexing**, see §7-B4.)

The broken OBO-at-login path (`auto_provision_connection_credentials`,
`exchange_obo_token`) is effectively dead weight for this kind of registration — we
should either delete it or document that it requires Expose-an-API.

### 6c. Still TODO to fully close the loop
- **Fabric server host + warehouse name** were not provided — needed to run a real
  `SELECT 1` / table-list with each user's delegated token and prove
  per-user permission differences (demo1 vs demo2). Both tokens currently carry the
  same `scp`; row/table differences come from Fabric-side grants, which we can only
  see by querying. Drop the host in §5 and I'll run it (needs pyodbc + ODBC Driver 18
  in the sandbox — will verify install + reachability to `*.datawarehouse.fabric.microsoft.com`).

---

## 7. Enablement design space (your thoughts go here)

Drop your thoughts on how you want this to work. To frame it, here are the
decision points I see (no recommendation locked in — just the levers):

**A. How does the admin get a delegated token?**
- A1. Force the admin through the same interactive **Connect** flow (2a) as everyone
  else — simplest, no app-reg changes, but admin must click "Connect" once.
- A2. Make **OBO-at-login** actually work for admins (and everyone) — requires the
  app-reg to "Expose an API" so the login token's `aud` matches the connection app
  (the §2b prerequisite). Possibly needs login + connection to share one app reg, or
  a configured `LOGIN_SCOPE`.

**B. When is the service-principal fallback allowed?**
- B1. Keep it (current) — admins silently use the SP. (This is what you want to change.)
- B2. Per-connection toggle: "no system fallback — admins must use their own creds
  too" → admin with no personal token gets 403/Connect-prompt instead of SP.
- B3. Remove SP fallback entirely for `user_required` connections (strictest).
- B4. Separate the SP's two jobs: it's still used for **schema indexing / overlay
  sync** (background, no user context) but **never** for serving an interactive
  query. (Note: indexing currently relies on SP creds — see
  `data_source_service.py` refresh paths.)

**C. Scope of the change**
- Just Fabric, or all Entra OBO types (`powerbi/sharepoint/onedrive`) too?

Your notes:
> 

---

## 8. Open questions for you
1. Are the **login app** and the **Fabric connection app** the same Entra app
   registration, or two different ones? (Decides whether OBO-at-login can ever work
   vs. needing the Connect flow.)
2. Should the admin's *own* delegated identity be used for **indexing/schema
   refresh** too, or only for interactive queries (keeping SP for background jobs)?
3. When an admin has no personal token yet, what's the desired UX — block with a
   "Connect" prompt, or hard 403?
4. Is the goal also to *prevent* accidental principal use (audit/compliance), or
   just to *offer* admin OBO as an option?

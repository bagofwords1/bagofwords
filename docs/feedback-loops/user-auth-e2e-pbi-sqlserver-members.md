# Feedback Loop — user-required auth end to end: Power BI (OBO), SQL Server, login & members

Live sandbox run (2026-09-06) driving three real use cases through the product
UI against a real Azure tenant (`bow14.onmicrosoft.com`) and a real SQL Server
2022 container:

1. **Power BI with "Require user authentication"** — service-principal indexing,
   per-user OBO sign-in, DAX executed as the signed-in user, RLS honoured.
2. **SQL Server (testcontainer)** — the same connector twice: shared system
   credentials, and `auth_policy=user_required` where each member supplies
   their own SQL login.
3. **Login and members** — password login, invites, roles, groups, removal,
   and the privilege boundary between `member` and `admin`.

Secrets came from env vars only (`PBI_TENANT_ID`, `PBI_CLIENT_ID`,
`PBI_CLIENT_SECRET`, demo user passwords, `ANTHROPIC_API_KEY`). Nothing in this
document or the repo carries a credential.

## Sandbox

Per the `sandbox-feedback-loop` skill, plus two things this run needed:

```bash
# ODBC driver for the MSSQL connector (the Docker image installs it; a bare
# sandbox does not) — without it every MSSQL connection test fails
curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/ubuntu/24.04/prod noble main" \
  > /etc/apt/sources.list.d/microsoft-prod-24.04.list
apt-get update && ACCEPT_EULA=Y apt-get install -y unixodbc msodbcsql18

# SQL Server fixture (dockerd is not running by default in the sandbox)
sudo dockerd &
docker run -d --name bow-mssql -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD=... -p 1433:1433 \
  mcr.microsoft.com/mssql/server:2022-latest
```

`SalesDB` fixture: `dbo.Orders` (10 rows, total 9 600 — US 4 200 / EMEA 2 950 /
APAC 2 450) and `dbo.Customers` (5 rows). Three SQL logins, deliberately
unequal so per-user auth has something to prove:

| login | grants |
|---|---|
| `bow_svc` | `db_datareader` on `SalesDB` (the shared system credential) |
| `analyst1` | SELECT on `Orders` **and** `Customers` |
| `analyst2` | SELECT on `Orders` only — `Customers` is denied (verified: `Msg 229 … SELECT permission was denied on the object 'Customers'`) |

Local users: `admin@example.com` (admin), `analyst-one@` / `analyst-two@`
(members, invited and registered through the real invite link).

## 1. Power BI, `auth_policy=user_required`

Created through the UI (`/agents/new` → Add Connection → Power BI), service
principal as System Credentials, **Require user authentication** on.

| Step | Result |
|---|---|
| Test connection (SP) | `Connected successfully. Found 19 model tables.` |
| Indexing | `connection_indexings.status=completed`, 19 model tables in 5.2s |
| `unreadable_datasets` | `geo` and `rls_sales` in workspace `verify-rls`, reason `COLUMNSTATISTICS failed: not authorized to query (Build permission required, or RLS with no effective identity)` — the documented SP-vs-RLS limitation, reported rather than swallowed |
| demo1 OBO sign-in → `analyst-one` | connect gate success, overlay **19 tables** incl. `rls_sales/Sales` |
| demo2 OBO sign-in → `analyst-two` | connect gate success (`Verified query access on dataset 'deals2'`), `RefreshUserPermissions` HTTP 200, overlay **20 tables** incl. `shared_orders/Orders` |

Per-user overlays land in `user_data_source_tables`, one row set per user —
demo2's extra `shared_orders/Orders` (item-level share, no workspace role) is
present only for demo2, exactly as `powerbi-obo-rls.md` describes.

### DAX through the chat UI

`analyst-one`, prompt *"Using the SalesPush model, show total Revenue and order
count per Region as a table"*:

| Region | product answer | REST ground truth |
|---|---|---|
| East | $193,327 / 95 | 193327 / 95 |
| North | $156,612 / 72 | 156612 / 72 |
| South | $179,041 / 70 | 179041 / 70 |
| West | $159,270 / 63 | 159270 / 63 |

Executed code (`steps.code`) was real DAX through the product's client:

```dax
EVALUATE SUMMARIZECOLUMNS(Sales[Region], "TotalRevenue", SUM(Sales[Revenue]),
                          "OrderCount", COUNTA(Sales[OrderID]))
```

### RLS, both directions

`rls_sales/Sales` is user-contributed, so it lands `is_active=False` and needed
an admin to activate it in the agent's Tables view (the documented gate — it
behaved exactly as described). Then the same prompt to both members:

| BOW user → Entra identity → RLS role | answer |
|---|---|
| `analyst-one` → demo1 → `USOnly` | **US $9,000.00, 3 rows** |
| `analyst-two` → demo2 → `EMEAOnly` | **EMEA $4,600.00, 3 rows** |

Disjoint slices of the same 6-row model, through the product's own client
stack, matching the REST ground truth taken independently with each user's
delegated token. No cross-user leak in either direction.

**Not exercised:** the interactive Microsoft login page. Chromium cannot reach
`login.microsoftonline.com` from this sandbox — `ERR_CONNECTION_RESET`, with or
without `--proxy-server` pointed at the agent proxy, while `curl` to the same
host succeeds. The delegated token was therefore obtained by ROPC and fed
through the product's real credential path
(`tools/agent/e2e_powerbi_obo_rls.py`: upsert `UserConnectionCredentials` →
`ConnectionService.test_user_connection` → `DataSourceService.get_user_data_source_schema`),
same as the earlier OBO loops. Everything after the browser redirect is product
code; the redirect itself is untested here.

## 2. SQL Server, both auth policies

**System credentials** (`bow_svc`, `auth_policy=system_only`): connection test
`Connected successfully. Found 2 tables`, indexing completed, and the chat
answer to *"total order amount per region"* was APAC $2,450 / EMEA $2,950 /
US $4,200 — the seeded totals to the cent, from generated T-SQL
(`SELECT Region, SUM(Amount) … GROUP BY Region`).

**Per-user credentials** (`auth_policy=user_required`): a second connection with
the same system credential for indexing and *Require user authentication* on.
Each member saw a **Sign in** badge on the agent, entered their own SQL login,
and got `Connected successfully - Successfully connected to SQL Server`.

The per-user overlay is the load-bearing result:

| user | SQL login | `user_data_source_tables` |
|---|---|---|
| `analyst-one` | `analyst1` | `dbo.Customers`, `dbo.Orders` |
| `analyst-two` | `analyst2` | `dbo.Orders` **only** |

Asked *"list every customer name and their signup date from the Customers
table"*, `analyst-two`'s agent could not see the table at all and said so —
structural denial (the table is absent from that user's context), not a
runtime permission error. `analyst-one` answered it fine. Live
`sys.dm_exec_sessions` confirmed the queries reached SQL Server as `analyst1`
and `analyst2` respectively, not as `bow_svc`.

One thing to be careful about when writing this kind of test: asked for
"customers per region", the model answered correctly for `analyst-two` **from
`Orders` alone** (`COUNT(DISTINCT CustomerID)`) while its prose claimed it had
joined `Customers`. The numbers were right and no unauthorised table was
touched — but the narration was wrong, and a test that trusted the prose would
have reported a security hole that does not exist. Always read `steps.code`.

## 3. Login and members

Through the UI unless noted.

| Check | Result |
|---|---|
| Wrong password | stays on sign-in, `Invalid credentials` |
| Unknown user | rejected |
| Correct password | lands on `/` |
| Anonymous → `/settings/members` | redirected to `/users/sign-in?redirect=/settings/members` |
| "Sign in with Microsoft" button | rendered (config `auth.mode: hybrid` + entra OIDC) |
| Invite → register via invite link | both members joined the org, role `member` |
| Member view of `/settings/members` | read-only roster: no Add Member, no Remove, no role editor, no Roles/Groups/Service Accounts/Quotas tabs |
| Member calling admin APIs | `PUT` role, `DELETE` member, invite-link, `POST` member, `GET /llm/providers` → **403** `Permission denied: this action needs 'manage_members'` / `'manage_llm'` |
| Role assignment (admin) | the Role cell is a multi-select over `role_assignments`; adding `admin` to a member makes `whoami` report `admin` and admin APIs return 200. `memberships.role` is a separate legacy column and does not move — effective permissions resolve from `role_assignments` |
| Groups | created `AllFabric`, added a member; `group_memberships` row written, `Source: Manual` (directory-synced groups are distinguished here) |
| Remove member | row gone; the removed user's existing JWT → **401** immediately, and re-login → **403** `Your account has been disabled` |

**Not exercised:** the Entra SSO sign-in itself, same browser-egress limitation
as above; group sync from the `AllFabric` / `MinimalFabric` Entra groups
therefore could not be observed end to end.

## Findings

### 1. `POST /api/llm/providers/{id}/toggle` always returned 500 — FIXED here

`LLMProvider.models` is `lazy="joined"`, so the SELECT returns one row per
model and `scalar_one_or_none()` raised *"The unique() method must be invoked
on this Result"*. Every sibling query in `llm_service.py` calls `.unique()`;
line 950 did not. Effect: **an admin could never enable or disable an LLM
provider** — every attempt 500'd, for preset and customer-managed providers
alike. Reproduced on both, fixed by adding `.unique()`, pinned by
`tests/e2e/test_llm_providers.py::test_toggle_provider_enables_and_disables`
(fails without the fix) and `…::test_toggle_provider_rejects_unknown_provider`.

This one bit the loop directly: the sandbox's seeded provider had a dead API
key and there was no way to turn it off from the product.

### 2. A preset provider's default model cannot be pinned

On a **preset** (BOW-managed) provider, `_sync_preset_models` re-points the org
default to the catalog's default on every `GET /api/llm/models`
(`default_promotion_allowed = provider_had_default or not has_enabled_default`,
`llm_service.py` ~2519-2540). Measured:

```
POST /api/llm/models/<haiku>/set_default   -> {"success": true}
sqlite: is_default -> claude-haiku-4-5-20251001
GET  /api/llm/models                       -> (no write intended)
sqlite: is_default -> claude-sonnet-5
```

The same mechanism overrides bow-config: `default_llm` declaring exactly one
model with `is_default: true` (Haiku) produced five enabled models with
`claude-sonnet-5` as default. An operator who pins a cheap model for cost
control silently runs a more expensive one, and the UI's model picker defaults
to it. Customer-managed providers are unaffected (`set_default` survives the
listing there — verified), so the workaround is to add the provider through
`/settings/models` rather than through `default_llm`.

Not fixed here: the "preset providers mirror the catalog" rule is deliberate
and the right fix (honour an explicit admin/config default over catalog
opinion) is a product decision.

### 3. SSO redirect_uri ignores `base_url` **and** `X-Forwarded-Host`

`_get_redirect_uri` (`app/services/auth_providers.py`) uses
`derive_request_base_url`, which takes the scheme from `X-Forwarded-Proto` but
the host from the `Host` header only. Measured with `base_url:
http://localhost:3000` configured:

| request | redirect_uri returned |
|---|---|
| direct to `:8000` | `http://localhost:8000/api/auth/entra/callback` |
| through the frontend proxy at `:3000` | `http://127.0.0.1:8000/api/auth/entra/callback` |
| `X-Forwarded-Host: bow.example.com`, `X-Forwarded-Proto: https` | `https://127.0.0.1:8000/api/auth/entra/callback` |

The last row is the problem: forwarded scheme, internal host — a pair that can
never be a registered redirect URI, so SSO login fails with AADSTS50011 behind
any reverse proxy that rewrites `Host` (the common
`proxy_set_header Host $upstream` setup). The connection-OAuth flow does not
have this shape: `routes/connection_oauth.py:220` builds its redirect from
`settings.bow_config.base_url`. Ignoring config here is intentional per the
docstring ("must match the initiating domain"), but consulting
`X-Forwarded-Host` — as `derive_base_url` in the same module already does —
would close the gap.

### 4. MSSQL cannot use an encrypted connection to a self-signed server

`MSSQLClient` pins `TrustServerCertificate=no` and lists it as a protected ODBC
key, so a SQL Server presenting a self-signed or internal-CA certificate (the
default for a container, and common on-prem) can only be reached by turning
**Encrypt off** entirely. The safer middle setting — encrypt, don't verify — is
unreachable, so the operator's only option is the least safe one. This run had
to disable encryption to connect at all.

### 5. Smaller observations

- A user-contributed Power BI table's activation state was inconsistent between
  the two contributed models: `rls_sales/Sales` landed `is_active=0` (needing
  admin activation, as documented) while `shared_orders/Orders` landed
  `is_active=1`.
- `GET /api/llm/providers` filters to enabled providers, so a disabled provider
  disappears from the listing rather than showing as off — worth knowing when
  asserting on toggle behaviour.
- The narration/execution mismatch described in §2: prose claimed a join the
  generated code did not perform.

## Reproducing

```bash
# stack (see the sandbox-feedback-loop skill for the full recipe)
cd backend && BOW_DATABASE_URL=sqlite:///db/app.db BOW_ENCRYPTION_KEY=<fixed> \
  BOW_CONFIG_PATH=<config with auth.mode hybrid + entra oidc> uv run python main.py
cd frontend && yarn dev

# per-user Power BI OBO sign-in for a given local user
BOW_RLS_LOCAL_EMAIL=analyst-one@example.com \
BOW_RLS_USER_EMAIL=$DEMO1_EMAIL BOW_RLS_USER_PASSWORD=$DEMO1_PASSWORD \
  uv run python ../tools/agent/e2e_powerbi_obo_rls.py

# regression tests for finding 1
BOW_DATABASE_URL=sqlite:///db/app.db uv run pytest tests/e2e/test_llm_providers.py -k toggle_provider
```

A fixed `BOW_ENCRYPTION_KEY` is required — without it each restart invents one
and every stored connection credential (and JWT) becomes undecryptable.

---
name: add-connection-type
description: Add a new data source / connection type (e.g. a SQL Server-like database) end to end — client with get_schemas/execute_query, config + credentials schemas, registry entry, icon, tests — plus the verification steps. Also covers adding or updating an MCP connector (a preset, not a new type). Use when adding or significantly extending a connector.
---

# Add a new connection type

A connector is **registry-driven**: the frontend form, auth variants, and
client resolution all derive from one entry in
`backend/app/schemas/data_source_registry.py` (`REGISTRY`). You almost never
touch frontend form code.

> **Adding an MCP server?** You almost never need a new type — add a **preset**
> instead. Jump to [Adding or updating an MCP connector](#adding-or-updating-an-mcp-connector-preset).

Before writing anything, read one reference implementation end to end:
`postgresql` (plain SQL DB), `mssql_client.py` (ODBC + Kerberos variants), or
`clickhouse` — pick whichever is closest to the new type.

## Files to create / update (in order)

1. **Client** — `backend/app/data_sources/clients/<type>_client.py`, extending
   the base in `clients/base.py`. Base contract:
   - `get_schemas()` → tables/columns catalog (some clients also expose
     `get_tables()` as a finer-grained step; follow the reference client).
     Support a `progress_callback` kwarg if enumeration is slow — the base
     inspects for it.
   - `execute_query(...)` → rows (a `query` alias is provided by the base).
   - connect/test-connection behavior mirroring the reference client, so the
     "Test connection" button works.
   Long-running/laggy calls: keep them sync — the base provides async
   wrappers (`aget_schemas`, …).
2. **Config + credentials schemas** —
   `backend/app/schemas/data_sources/configs.py`: a `<Type>Config` (host,
   port, database, …) and one `<Type>...Credentials` class **per auth
   variant** (userpass, token, kerberos, none…). These Pydantic schemas ARE
   the frontend form — field names, types, defaults and descriptions render
   directly in `ConnectForm.vue` via `GET /available_data_sources`.
3. **Registry entry** — `backend/app/schemas/data_source_registry.py`:
   add to `REGISTRY` with `type`, `title`, `description`,
   `config_schema=<Type>Config`,
   `credentials_auth=AuthOptions(default=..., by_auth={...})` (each
   `AuthVariant` declares `scopes=["system","user"]` — include `"user"` only
   if per-user credentials make sense), and **always set `client_path`
   explicitly** (`"app.data_sources.clients.<type>_client.<Type>Client"`).
   The dynamic-naming fallback exists but has caused real bugs — the explicit
   path is the contract. Use `dev_only=True` while incubating.
4. **Driver dependency** — `cd backend && uv add <driver>` (updates
   `pyproject.toml` + `uv.lock`). Prefer pure-python drivers; if a system
   library is required (ODBC, kerberos), it must also be added to the root
   `Dockerfile` and called out in the PR description.
5. **Icon** — drop `frontend/public/data_sources_icons/<type>.png|svg` and
   map it in `frontend/components/DataSourceIcon.vue`.
6. **Tests**:
   - Unit: `backend/tests/unit/test_<type>_client.py` — mock the **driver**
     boundary only (see `test_druid_client.py`); assert schema-shape and
     query-dispatch behavior per `backend/tests/AGENTS.md`.
   - Integration: add the type to `DATA_SOURCES` in
     `backend/tests/integrations/ds_clients.py`. If a docker image exists,
     add a `CONTAINER_REGISTRY` entry (testcontainers) so it runs without
     live credentials; otherwise credentials go in `integrations.json`
     (local only — CI restores it from the `INTEGRATIONS_JSON_B64` secret;
     never commit it).

## Verification steps (all of them)

```bash
cd backend
# 1. Registry resolves: entry present, client imports via client_path
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('<type>'))"
# 2. Unit tests
uv run pytest tests/unit/test_<type>_client.py -v
# 3. Generic data-source e2e suite still green (create/update/delete flows)
uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
# 4. Integration test against a real instance (container or creds)
uv run pytest tests/integrations/ds_clients.py -k "<type>" -v
```

5. **Live UI pass**: `tools/agent/boot_stack.sh` + `seed_org.py`, then in the
   app: create the connection → "Test connection" succeeds → Tables Selector
   lists tables → run a prompt that queries it. Screenshot the connect form
   and the tables list (**ui-evidence** skill) — the form is
   schema-generated, so this is also the review of your config schemas.
6. Record the whole loop as `docs/feedback-loops/<type>-connector.md`
   (**sandbox-feedback-loop** skill) so the next agent can re-verify.

## Pitfalls

- Missing `client_path` → silent dynamic-import fallback that breaks with
  confusing errors on any module rename.
- Skipping the `user` scope decision: `user_required` auth-policy sources
  need per-user overlays to behave (see
  `docs/feedback-loops/fabric-obo-second-admin-tables.md` for how that bites).
- Registry `description` and config field descriptions are user-facing copy —
  write them like product text, not comments.
- `is_connection=False` is only for tool providers (MCP-style); leave it
  unset for data sources or schema indexing will skip your type.

## Adding or updating an MCP connector (preset)

An MCP server almost never needs a new *type* — the runtime, DCR, and OAuth all
gate on `connection.type == "mcp"`. Add a **preset** instead: a named
`McpPreset` in `MCP_PRESETS`
(`backend/app/schemas/data_source_registry.py`) that resolves to `type="mcp"`.
It carries only branding + a form spec — no client, no config/credentials
schema, no per-type tests, no new dispatch sites.

### 1. Add / edit the preset entry

```python
McpPreset(
    key="x", title="X",
    server_url="https://api.x.com/mcp",
    transport="streamable_http",           # streamable_http | sse
    auth="oauth_app",                      # default form auth: oauth(DCR) | oauth_app | bearer
    allowed_auth=["oauth_app", "bearer"],  # modes the form offers, in FORM vocab
                                           # (none|bearer|api_key|dcr|oauth_app); None = all
    oauth_defaults=McpAuthDefaults(        # prefilled when oauth_app is chosen — these are
        authorize_url="https://twitter.com/i/oauth2/authorize",  # provider constants, editable
        token_url="https://api.x.com/2/oauth2/token",
        scopes="tweet.read tweet.write users.read offline_access",
        audience=None,                     # RFC 8707 resource, only if the server needs it
    ),
    sample_tools=["get_users_by_username", "search_posts"],  # illustrative preview ONLY
    description="Posts, users, search and trends from X.",    # user-facing subtitle
)
```

Pick the auth shape:
- **DCR** (`auth="oauth"`, e.g. Notion/Linear/Atlassian): omit `oauth_defaults`
  (endpoints are auto-discovered, RFC 9728/8414); `allowed_auth=["dcr"]`. Add the
  server + AS host to `allowed_dcr_hosts()` coverage via the preset URL.
- **oauth_app** (X/GitHub/Gmail/Drive): provider endpoints are invariant, so fill
  `oauth_defaults`; only client_id/secret are per-deployment. `allowed_auth`
  usually `["oauth_app"]` (X also allows `"bearer"`).
- **bearer**: a per-user token/PAT; no `oauth_defaults`.

`GET /connectors/catalog` serves presets via `mcp_presets()` → `model_dump()`,
so any new field flows to the form with **no route change**.

### 2. Icon

Drop `frontend/public/data_sources_icons/<key>.svg` and map `key → file` in
`frontend/components/DataSourceIcon.vue` (`CONNECTOR_ICON_FILE`).

### What the form does automatically (no frontend work)

`MCPConnectionForm.vue` is preset-aware: it prefills `oauth_defaults`, gates the
auth dropdown by `allowed_auth`, hides the known fields (server URL, transport,
OAuth endpoints) under **Advanced** for presets (a custom URL shows them inline),
renders the `description` subtitle + `sample_tools` chips, offers **Create a
public agent** for OBO modes (oauth_app/dcr), and treats a 401/auth-challenge on
Test as "reachable — sign-in required". Edit-mode prefill reads `credentials_meta`
from `GET /connections/{id}` (non-secret OAuth fields only — secrets never leave
the server).

### Gotchas

- `access_type=offline` is Google-only — the authorize route
  (`connection_oauth.py`) gates it on `provider_name == "google"`; other
  providers (e.g. X) reject the unknown param. Don't reintroduce it globally.
- Tools for `user_required` presets are discovered **per user on first sign-in**
  (no admin token at config time), not at connection create.
- `allowed_auth` uses the FORM vocabulary (`dcr`), not the catalog `auth` value
  (`oauth`). Keep them consistent.

### Tests + verify

- Extend `backend/tests/unit/test_mcp_presets.py` — pin the contract (auth,
  server_url, transport, `oauth_defaults`, `allowed_auth`, `sample_tools`) and
  catalog serialization.
- Live pass: catalog tile → prefilled form → Test/Verify → create → tools
  discovered on first sign-in. Screenshots via the **ui-evidence** skill.
- Reference loops: `docs/feedback-loops/mcp-preset-form-defaults.md` and
  `docs/feedback-loops/x-mcp-preset.md`.

---
name: add-connection-type
description: Add a new data source / connection type (e.g. a SQL Server-like database) end to end — client with get_schemas/execute_query, config + credentials schemas, registry entry, icon, tests — plus the verification steps. Use when adding or significantly extending a connector.
---

# Add a new connection type

A connector is **registry-driven**: the frontend form, auth variants, and
client resolution all derive from one entry in
`backend/app/schemas/data_source_registry.py` (`REGISTRY`). You almost never
touch frontend form code.

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

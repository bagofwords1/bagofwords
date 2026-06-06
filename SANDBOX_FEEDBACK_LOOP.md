# Sandbox Feedback Loop — Connectionless Agents

A reproducible loop for developing and verifying the **"create an agent with
no connections"** feature (`/agents/new`) against the sandbox config, without
Docker or a real Postgres.

## What the feature does

An "agent" in this codebase is a `DataSource` (domain) with a many-to-many
relationship to `Connection`s. Previously, creating one **required** either
linking to an existing connection or supplying `type` + `config` to build a new
one. This change adds a third mode: a **connectionless agent** (no data
connections) for instruction / context-only use.

## Environment

- Python **3.12** is required (`main.py` uses 3.12-only f-string syntax). The
  default `python3` here is 3.11, so a 3.12 venv is used.
- Tests default to **SQLite** (`TEST_DB=sqlite`); no Docker needed.
- The **sandbox config** (`configs/bow-config.sandbox.yaml`) is used because it
  enables `allow_uninvited_signups: true` and `auth.mode: local_only`, so the
  e2e fixtures can register/login users (the dev config 404s on
  `/api/auth/register`).

## One-time setup

```bash
cd backend
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip

# psycopg2 (source) and databricks-sql-connector pull native build deps that
# aren't needed for the SQLite test loop. Use psycopg2-binary (already pinned)
# and skip them:
grep -v -iE "^psycopg2==|^databricks-sql-connector==" \
  requirements_versioned.txt > /tmp/reqs_filtered.txt
pip install -r /tmp/reqs_filtered.txt
```

## The loop

```bash
cd backend
source venv/bin/activate
export BOW_DATABASE_URL="sqlite:///db/app.db"
export BOW_CONFIG_PATH="/home/user/bagofwords/configs/bow-config.sandbox.yaml"

# Fast inner loop — the connectionless agent tests
python -m pytest tests/e2e/test_connectionless_agent.py -q

# Regression guard — existing data-source / agent suites
python -m pytest \
  tests/e2e/test_data_source.py \
  tests/e2e/test_agent_yaml_apply.py \
  tests/e2e/rbac/test_rbac_data_sources.py \
  tests/unit/test_agent_manifest.py -q
```

## Tests added

`tests/e2e/test_connectionless_agent.py`:

1. `test_create_agent_with_no_connections` — POST `/api/data_sources` with only
   a `name` succeeds and returns an agent with `connections == []`, and it
   appears in the listing.
2. `test_create_agent_with_empty_connection_ids` — explicit `connection_ids: []`
   is also treated as connectionless.
3. `test_create_agent_half_configured_connection_is_rejected` — supplying
   `type` without `config` still 422s (the one combination that stays invalid).

## Results (last run)

| Suite | Result |
|---|---|
| `test_connectionless_agent.py` (new) | **3 passed** |
| `test_data_source.py` + `test_agent_yaml_apply.py` + RBAC + `test_agent_manifest.py` | **24 passed** |

## Changes under test

**Backend**
- `app/schemas/data_source_schema.py` — `validate_connection_ids_or_type` now
  allows the "nothing provided" case (connectionless); only `type` without
  `config` is rejected.
- `app/services/data_source_service.py` — `create_data_source` gained a third
  branch (no existing connections, no `type`) that creates the domain with no
  connections and skips connection validation / background indexing.

**Frontend** (verified by review; no automated FE harness in repo)
- `pages/agents/new/index.vue` — connections are optional: label says
  "(optional)", the submit ("Save & Continue") block always shows, the
  AddConnectionModal is no longer force-opened when no connections exist, and a
  connectionless agent skips the table-selection step (routes straight to the
  context step).
- `pages/agents/index.vue` — the "Create Agent" button no longer requires
  `connections.length > 0`.

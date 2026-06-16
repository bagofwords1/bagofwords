# Sandbox Feedback Loop — Apache Druid connector

Adds an **Apache Druid** data source connector, following the same plugin
pattern as PostgreSQL / Pinot: a `DataSourceClient` subclass, a Pydantic config
schema, and a `REGISTRY` entry. Druid speaks Calcite SQL over an HTTP endpoint,
so the client uses the `pydruid` DB-API driver and discovers its catalog from
`INFORMATION_SCHEMA.COLUMNS` — the standard metadata the engine exposes.

This doc is the runnable feedback loop used to build and validate the connector
in a fresh cloud sandbox.

---

## What was added

| Layer | File | Change |
|-------|------|--------|
| Client | `backend/app/data_sources/clients/druid_client.py` | New `DruidClient(DataSourceClient)` |
| Config | `backend/app/schemas/data_sources/configs.py` | New `DruidConfig` (credentials reuse `SQLCredentials`) |
| Registry | `backend/app/schemas/data_source_registry.py` | Import `DruidConfig`; add `"druid"` entry |
| Driver | `backend/requirements_versioned.txt` | `pydruid==0.6.9` |
| Icon | `frontend/public/data_sources_icons/druid.png` | Official Druid logo (520×400) |
| Tests | `backend/tests/unit/test_druid_client.py` | 18 unit tests |

No frontend code change is needed: the connect form and `DataSourceIcon.vue`
render dynamically from the registry entry and the `druid.png` filename. The
registry's dynamic resolver maps type `"druid"` → `druid_client.DruidClient`,
so `client_path` stays `None`.

---

## Design decisions

- **Driver: `pydruid` DB-API** (`pydruid.db.connect`), mirroring the Pinot
  client. Chosen over a fragile third-party SQLAlchemy dialect or hand-rolled
  HTTP. Imported **lazily inside `connect()`** (like the Spark client imports
  `pyspark`) so the module imports even when the driver isn't installed, and so
  the unit tests can inject a fake `pydruid` via `sys.modules`.
- **Schema discovery via `INFORMATION_SCHEMA.COLUMNS`.** Druid datasources live
  in the `druid` schema; `sys` and `INFORMATION_SCHEMA` are engine internals and
  are excluded by default. Tables are named `schema.table` (e.g.
  `druid.wikipedia`), consistent with the Postgres client. An optional
  comma-separated `schema` config narrows discovery (deduped, order-preserved).
- **Defaults:** port `8082` (Broker SQL; use `8888` for the Router),
  `http` scheme, path `/druid/v2/sql/`. Auth is optional HTTP basic
  (`userpass`), matching unsecured-cluster reality.
- **`get_schema()` is obsolete** (raises `NotImplementedError`) — the same
  contract as every other SQL client; `get_schemas()` / `get_tables()` are used.

---

## Environment setup (fresh sandbox)

The app targets **Python 3.12**. The sandbox default `python` may be 3.11 — use
3.12 explicitly.

```bash
cd backend
python3.12 -m venv /tmp/venv312
/tmp/venv312/bin/pip install -q --upgrade pip

# Heavy/native packages aren't needed for the unit-level repro. pydruid is
# excluded too: the tests mock it via sys.modules (no live Druid required).
grep -ivE '^psycopg2|^pyspark|^thrift|^pyodbc|^grpcio-tools|^confluent-kafka|^snowflake|^cx[-_]Oracle|^oracledb|^pymssql|^sqlalchemy-bigquery|^google-cloud|^pydruid' \
  requirements_versioned.txt > /tmp/reqs_lite.txt
/tmp/venv312/bin/pip install -q -r /tmp/reqs_lite.txt

# Required by bow-config.dev.yaml (database.url: ${BOW_DATABASE_URL})
export BOW_DATABASE_URL="sqlite:///db/app.db"
mkdir -p db
```

---

## Loop A — Unit validation (no live Druid needed)

Self-contained. A fake `pydruid.db` module is injected so `DruidClient.connect()`
yields a controllable cursor. Covers connect-kwarg construction, schema parsing,
`execute_query → DataFrame`, `INFORMATION_SCHEMA` discovery (FQN mapping,
system-schema exclusion, explicit schema filter), `test_connection`
success/failure, and the registry wiring (`resolve_client_class("druid")` →
`DruidClient`, `DruidConfig` validation).

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
/tmp/venv312/bin/python -m pytest tests/unit/test_druid_client.py -v
```

**Observed (PASS):**

```
================= 18 passed, 273 warnings in 155.31s =================
```

> The ~2.5 min wall time is the test harness importing the full app at
> collection, not the tests themselves.

Iterate here: edit the client/config/registry and re-run.

---

## Loop B — Live confirmation (real Druid, optional)

Confirms against a real broker. `pydruid` is required here (it's mocked in Loop
A). The official Druid quickstart ships a `wikipedia` datasource.

```bash
/tmp/venv312/bin/pip install pydruid==0.6.9

cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
/tmp/venv312/bin/python - <<'PY'
from app.data_sources.clients.druid_client import DruidClient
c = DruidClient(host="localhost", port=8888)   # Router; use 8082 for Broker
print(c.test_connection())
for t in c.get_schemas():
    print(t.name, [col.name for col in t.columns])
print(c.execute_query("SELECT COUNT(*) AS n FROM druid.wikipedia"))
PY
```

Expected: `{'success': True, ...}`, the discovered datasources with columns, and
a row count. (Not run in this sandbox — no live Druid cluster.)

---

## What this proves

- The connector is fully wired: the registry resolves `"druid"` to the new
  client, `DruidConfig` validates with sane defaults, and the UI will render the
  form + icon with no frontend change.
- Query and schema-discovery logic is correct against the documented Druid SQL /
  `INFORMATION_SCHEMA` contract, verified end-to-end against a faked driver.
- The connector degrades gracefully when `pydruid` isn't installed (lazy import)
  and on connection/query errors (`get_tables` → `[]`, `test_connection` →
  `{"success": False, ...}`).

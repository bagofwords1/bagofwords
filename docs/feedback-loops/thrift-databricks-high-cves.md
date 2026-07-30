# Feedback Loop — "Snyk reports 3 High CVEs in `thrift@0.22.0`"

A scheduled Snyk scan of the backend dependency graph surfaced **3 High-severity
vulnerabilities**, all in `thrift@0.22.0`, pulled in transitively by
`databricks-sql-connector@4.3.0`. This loop validates the finding, the fix
(bump the connector so it resolves a patched `thrift`), and that the app still
imports, boots, and answers its liveness probe afterward.

## Findings (validated)

| Snyk ID | Title | Severity | Fixed in |
|---|---|---|---|
| SNYK-PYTHON-THRIFT-18389009 | Improper Handling of Highly Compressed Data (Data Amplification) | High | thrift 0.24.0 |
| SNYK-PYTHON-THRIFT-18389257 | Improper Handling of Highly Compressed Data (Data Amplification) | High | thrift 0.24.0 |
| SNYK-PYTHON-THRIFT-18391455 | Improper Certificate Validation | High | thrift 0.24.0 |

Dependency path (all three): `databricks-sql-connector@4.3.0 → thrift@0.22.0`.

Snyk Code (SAST, `--severity-threshold=high`) and the frontend (`yarn.lock`,
1238 deps) were both **clean** — no high/critical. Only the backend graph had
findings.

## Root cause (validated)

`backend/pyproject.toml:51` pinned `databricks-sql-connector>=4.3.0,<4.4`, and
`databricks-sql-connector 4.3.0` hard-requires `thrift (>=0.22.0,<0.23.0)` (per
its published metadata). That upper bound makes the patched `thrift 0.24.0`
unreachable while the connector stays on the 4.3 line — a forced `thrift`
override would violate the connector's own constraint. The clean resolution is
to move the connector to a release whose `thrift` range already includes the
fix.

Checked on PyPI:

- `databricks-sql-connector 4.3.0` → `thrift <0.23.0,>=0.22.0` (vulnerable)
- `databricks-sql-connector 4.2.7` → `thrift <0.24.0,>=0.22.0` (still < 0.24.0)
- `databricks-sql-connector 4.4.0` → `thrift <0.25.0,>=0.24.0` ✅ pulls the fix

Diff between 4.3.0 and 4.4.0 is minimal — the `thrift` bump plus a `pandas`/`lz4`
`python_version` floor moving 3.8 → 3.10 (irrelevant; the project requires
`>=3.12`). `requires-python` becomes `>=3.10,<4.0`, still compatible.

## Loop A — deterministic reproduction (no external services)

Snyk resolves a `pip` target against the *installed* environment, so the scan
must run inside a venv that has the project's locked versions — otherwise it
silently reports the sandbox's stale base-image packages instead of the project.

```bash
cd backend
uv export --format requirements-txt --no-emit-project --no-hashes -o /tmp/req.txt
uv venv /tmp/scanvenv --python 3.12
uv pip install --python /tmp/scanvenv/bin/python -r /tmp/req.txt pip
source /tmp/scanvenv/bin/activate
cp /tmp/req.txt ./req_scan/requirements.txt   # any dir with just the file
export SNYK_TOKEN=...   # env var only, never commit
npx -y snyk@latest test --file=req_scan/requirements.txt --package-manager=pip
```

Observed **before** the fix:

```
Tested 235 dependencies for known issues, found 3 issues, 6 vulnerable paths.
  ✗ [High] ...THRIFT-18389009  in thrift@0.22.0
  ✗ [High] ...THRIFT-18389257  in thrift@0.22.0
  ✗ [High] ...THRIFT-18391455  in thrift@0.22.0
```

> Pitfall recorded so the next agent skips it: running Snyk with
> `--skip-unresolved` against a bare `requirements.txt` (no venv) resolves
> against the base image and reports **stale** packages (e.g. `cryptography
> 41.0.7`, `urllib3 2.6.3`) that are **not** in this project — the lock already
> ships `cryptography 49.0.0`, `urllib3 2.7.0`, etc. Always scan an installed
> venv built from the project lock.

## The fix

`backend/pyproject.toml` — one line:

```diff
-    "databricks-sql-connector>=4.3.0,<4.4",
+    "databricks-sql-connector>=4.4.0,<4.5",
```

Then `uv lock` (Python 3.12). Lock delta is exactly two packages:

```
Updated databricks-sql-connector v4.3.0 -> v4.4.0
Updated thrift                    v0.22.0 -> v0.24.0
```

Observed **after** the fix (same Loop A, venv updated from the new lock):

```
✔ Tested 235 dependencies for known issues, no vulnerable paths found.
```

## Loop A2 — app loads and health probe passes

```bash
cd backend
uv sync --frozen --extra dev --python 3.12
export BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
uv run alembic upgrade head            # build schema (112 tables)
uv run python - <<'PY'
from fastapi.testclient import TestClient
from main import app                    # imports full router graph (70 routes)
from app.data_sources.clients.databricks_sql_client import DatabricksSqlClient
with TestClient(app) as c:
    r = c.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    print("health:", r.status_code, r.json())
PY
```

Observed:

```
thrift: 0.24.0 | databricks-sql-connector: 4.4.0
OK: DatabricksSqlClient imports + constructs; databricks_sql.connect present
OK: FastAPI app imported; routes: 70
✅ Database connection successful
GET /health -> 200 {'status': 'ok'}
```

The `DatabricksSqlClient` uses the stable `databricks_sql.connect(...)` public
API (`app/data_sources/clients/databricks_sql_client.py:56`), which is unchanged
across 4.3 → 4.4.

## Loop B — live confirmation (not run)

The data-source connection tests in `tests/integrations/ds_clients.py` are the
live-credential leg; they **skip** without real warehouse credentials
(15 skipped, 0 failed in the sandbox). Exercising a real Databricks SQL
Warehouse round-trip against 4.4.0 is left to an environment that has those
secrets.

## What this proves / regression notes

- The three High CVEs are gone from the resolved graph; nothing else in the lock
  moved.
- The backend still imports, migrates, boots, connects to its DB, starts the
  scheduler, and serves `/health` — the same probe used by k8s, Docker
  healthcheck, and CI wait loops (`backend/main.py:246`).
- Pre-existing, unrelated noise seen during boot (Pydantic `schema`-field-shadow
  and `orm_mode` deprecation `UserWarning`s, `datetime.utcnow()` deprecations in
  migrations) reproduce without this change and are **not** introduced here.

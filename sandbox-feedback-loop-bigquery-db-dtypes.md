# Sandbox Feedback Loop — BigQuery "Please install the 'db-dtypes' package"

Reproduces and validates the reported bug: a **BigQuery** data source query
fails during **Create Data** with

```
Execution error: Please install the 'db-dtypes' package to use this function.
```

even though the SQL and the user's request are correct. This doc is the runnable
feedback loop used to confirm the root cause and verify the fix in a fresh cloud
sandbox — **no live BigQuery, credentials, or network to GCP required.**

---

## Root cause (validated)

`db-dtypes` is an **optional companion** of `google-cloud-bigquery` — it has
**never** been a hard dependency, in any version (including the latest). The
backend pins a recent client (`google-cloud-bigquery>=3.42.0,<3.43`,
`backend/pyproject.toml:45`) but **never declared `db-dtypes`**, so it was absent
from `uv.lock` and from the Docker runtime (`uv sync --frozen --no-dev`).

The BigQuery client converts every query result to a DataFrame:

- `backend/app/data_sources/clients/bigquery_client.py:90` — `execute_query`
- `backend/app/data_sources/clients/bigquery_client.py:127` — table enrichment
- `backend/app/data_sources/clients/bigquery_client.py:172` — basic table listing

In google-cloud-bigquery 3.42.x, `RowIterator.to_dataframe()` calls
`_pandas_helpers.verify_pandas_imports()` **unconditionally at the top**
(`table.py:2433`), which raises

```python
ValueError("Please install the 'db-dtypes' package to use this function.")
```

whenever `db_dtypes` can't be imported. So in this version the failure fires for
**every** BigQuery query, not only date/numeric results — which is why a plain
"daily new users" query failed. The model's own diagnosis in the thread was
correct: an **environment/dependency** gap, not SQL or question logic.

> Why a "latest version" still fails: the client version is irrelevant — the
> upstream package deliberately keeps `db-dtypes` as an extra
> (`google-cloud-bigquery[pandas]`). If you want pandas output, you must ship it.

---

## The fix

Declare the dependency explicitly (`backend/pyproject.toml:46`) and re-lock:

```toml
"google-cloud-bigquery>=3.42.0,<3.43",
"db-dtypes>=1.4.0,<1.5",
```

```bash
cd backend && uv lock        # -> adds db-dtypes v1.4.4 to uv.lock
```

The Docker build (`uv sync --frozen --no-dev`) then installs it at runtime.

---

## Environment setup (fresh sandbox)

The app targets **Python 3.12** (3.12 f-string syntax). The sandbox default
`python` may be 3.11 — `uv` selects 3.12 automatically from `requires-python`.

```bash
cd backend
pip install uv
UV_HTTP_TIMEOUT=300 uv sync --frozen --no-dev      # runtime deps (installs db-dtypes)
# for the pytest guard below, also: uv sync --frozen --extra dev
```

> `UV_HTTP_TIMEOUT=300` avoids a flaky 30s timeout on the large wheels
> (pyarrow / cryptography) in this sandbox.

---

## Loop A — Before/after repro (no live BigQuery needed)

`backend/scripts/repro_bigquery_db_dtypes.py` drives the **exact public
`to_dataframe()` guard** the client hits, via google-cloud-bigquery's real
`_EmptyRowIterator`. Exit code: `0` = fixed, `1` = bug reproduced.

```bash
cd backend

# AFTER (fixed env — db-dtypes installed from the updated lock)
./.venv/bin/python scripts/repro_bigquery_db_dtypes.py ; echo "exit=$?"

# BEFORE (reproduce the user's error by removing the dep)
uv pip uninstall db-dtypes
./.venv/bin/python scripts/repro_bigquery_db_dtypes.py ; echo "exit=$?"

# RESTORE (re-pin from the frozen lock)
uv sync --frozen --no-dev
```

**Observed:**

```
# AFTER
[env] db_dtypes import OK
[repro] FIX VERIFIED: to_dataframe() returned a DataFrame (rows=0) with no db-dtypes error
exit=0

# BEFORE
[env] db_dtypes import FAILED: ModuleNotFoundError("No module named 'db_dtypes'")
[repro] BUG REPRODUCED: to_dataframe() raised: Please install the 'db-dtypes' package to use this function.
exit=1
```

The `BEFORE` line is the **identical** message from the user's screenshot,
proving the repro matches the production failure and the dependency removes it.

---

## Loop B — Regression guard (pytest)

`backend/tests/unit/test_bigquery_db_dtypes_dependency.py` keeps it from
regressing — it asserts `db_dtypes` imports and that the same public
`to_dataframe()` path doesn't raise.

```bash
cd backend
BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true \
  ./.venv/bin/python -m pytest tests/unit/test_bigquery_db_dtypes_dependency.py -v
```

**Observed (PASS):**

```
tests/unit/test_bigquery_db_dtypes_dependency.py::test_db_dtypes_importable PASSED
tests/unit/test_bigquery_db_dtypes_dependency.py::test_bigquery_to_dataframe_does_not_raise_db_dtypes_error PASSED
2 passed
```

If `db-dtypes` is ever dropped from the lock again,
`test_bigquery_to_dataframe_does_not_raise_db_dtypes_error` fails with the exact
production `ValueError`, turning the suite red.

---

## What this proves

- The failure is a **declared-dependency** gap, fully independent of the
  google-cloud-bigquery version or the SQL.
- Adding `db-dtypes>=1.4.0,<1.5` to `pyproject.toml` + `uv lock` makes the exact
  `to_dataframe()` path the client uses succeed (Loop A), and a pytest guard
  prevents regression (Loop B).

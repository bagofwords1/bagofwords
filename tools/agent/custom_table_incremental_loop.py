#!/usr/bin/env python3
"""Run the custom-table incremental refresh loops in a disposable sandbox.

From backend:  uv run python ../tools/agent/custom_table_incremental_loop.py [pytest args]

Loop A always runs: the connector-agnostic core, the loader, the refresh
orchestration and run history, on the harness database with a real SQLite
source, plus the API tests. No credentials, live source or running app.

Loop B runs for each engine named in the environment (see
docs/feedback-loops/custom-table-incremental-refresh.md for the containers):

    INCR_LOOP_PG_URL     postgresql://user:pw@host:port/db
    INCR_LOOP_MSSQL_URL  mssql+pyodbc://user:pw@host:port/master?driver=...
    INCR_LOOP_MARIADB_URL mysql+pymysql://user:pw@host:port/db
    INCR_LOOP_ARIA_URL   http://127.0.0.1:8443   (tools/aria_operations mock)
    INCR_LOOP_SERVICENOW_URL http://127.0.0.1:9311 (tools/agent/fake_servicenow.py)

Point those only at disposable servers: Loop B reseeds and changes `shop`.
Set INCR_LOOP_REPORT to a path to collect the timings as JSON.
"""
import os
import subprocess
import sys
from pathlib import Path

backend = Path(__file__).resolve().parents[2] / "backend"
env = {**os.environ, "TESTING": "true"}
env.setdefault("BOW_DATABASE_URL", "sqlite:///db/app.db")
(backend / "db").mkdir(exist_ok=True)


def pytest(label: str, *args: str) -> int:
    print(f"\n=== {label} ===", flush=True)
    return subprocess.call(
        [sys.executable, "-m", "pytest", *args, "-q", "--timeout=1200", "--timeout-method=thread",
         "-p", "no:cacheprovider", *sys.argv[1:]],
        cwd=backend, env=env,
    )


failed = pytest(
    "Loop A — deterministic",
    "tests/unit/test_incremental_core.py",
    "tests/unit/test_incremental_refresh.py",
    "tests/unit/test_incremental_connectors.py",
    "tests/unit/test_fast_artifact_lifecycle.py",
    "tests/e2e/rbac/test_custom_table_incremental_api.py",
    "--db=sqlite",
)
live = [name for name in ("INCR_LOOP_PG_URL", "INCR_LOOP_MSSQL_URL", "INCR_LOOP_MARIADB_URL", "INCR_LOOP_ARIA_URL",
                             "INCR_LOOP_SERVICENOW_URL") if env.get(name)]
if live:
    failed |= pytest(f"Loop B — live ({', '.join(live)})",
                     "tests/integrations/test_custom_table_incremental_live.py", "--db=sqlite")
else:
    print("\nLoop B skipped: no INCR_LOOP_*_URL set.")
sys.exit(1 if failed else 0)

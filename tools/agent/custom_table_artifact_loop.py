#!/usr/bin/env python3
"""Run the artifact lifetime regression suite in a disposable sandbox.

From backend: uv run python ../tools/agent/custom_table_artifact_loop.py [pytest args]
No credentials, live source, running app or production paths are used.

The suite is the normal pytest module, run through the normal test harness:
``--db=sqlite`` by default (a private SQLite file per test), or ``--db=external``
against the PostgreSQL server in ARTIFACT_LOOP_POSTGRES_URL. Point that only at
a disposable server: the harness resets its ``public`` schema before each test.
"""
import os
import subprocess
import sys
from pathlib import Path

backend = Path(__file__).resolve().parents[2] / "backend"
env = {**os.environ, "TESTING": "true"}
postgres = env.get("ARTIFACT_LOOP_POSTGRES_URL")
if postgres:
    env["TEST_DATABASE_URL"] = postgres
    db = "external"
else:
    db = "sqlite"
sys.exit(subprocess.call(
    [sys.executable, "-m", "pytest", "tests/unit/test_fast_artifact_lifecycle.py", f"--db={db}", "-q",
     "--timeout=600", "--timeout-method=thread", *sys.argv[1:]],
    cwd=backend, env=env,
))

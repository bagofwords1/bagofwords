"""Verify SQL Server connection lifecycle against a real server.

Checks, from a separate monitoring connection watching sys.dm_exec_sessions,
that the app holds exactly the server sessions it should:

  1. A health probe (``atest_connection`` — the status sweep's path) leaves
     ZERO sessions behind and never touches the engine cache.
  2. ``execute_query`` keeps ONE pooled session open (pooling intact).
  3. Steady traffic REUSES that session (LIFO checkout — no round-robin
     fan-out across the pool).
  4. ``prune_idle_engines`` closes the idle pooled session server-side.
  5. The engine still works after pruning (next query just reconnects).
  6. A fresh TTL leaves a recently-used pool alone.

This guards the fixes for the field report of ``Data_Proj_App_User`` sessions
sitting idle for an hour whose last statement was ``SELECT 1`` — pooled
connections that pool_recycle never touched (it only fires at checkout) being
kept warm by the 5-minute status sweep, plus pyodbc's ODBC driver-manager
pooling holding sessions open after close().

Usage (spin up a disposable server first):

    docker run -d --name bow-mssql-test -e ACCEPT_EULA=Y \
        -e MSSQL_SA_PASSWORD='Bow!Test12345' -p 1433:1433 \
        mcr.microsoft.com/mssql/server:2022-latest
    cd backend && uv run python scripts/verify_mssql_connection_lifecycle.py

Env overrides: BOW_VERIFY_MSSQL_HOST / _PORT / _DATABASE / _USER / _PASSWORD.
Exits non-zero on the first failed check.
"""
import asyncio
import os
import sys
import time

import pyodbc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.data_sources import engine_pool
from app.data_sources.clients.mssql_client import MSSQLClient

HOST = os.environ.get("BOW_VERIFY_MSSQL_HOST", "127.0.0.1")
PORT = int(os.environ.get("BOW_VERIFY_MSSQL_PORT", "1433"))
DB = os.environ.get("BOW_VERIFY_MSSQL_DATABASE", "master")
USER = os.environ.get("BOW_VERIFY_MSSQL_USER", "sa")
PWD = os.environ.get("BOW_VERIFY_MSSQL_PASSWORD", "Bow!Test12345")

MON = pyodbc.connect(
    f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={HOST},{PORT};DATABASE={DB};"
    f"UID={USER};PWD={PWD};TrustServerCertificate=yes;APP=bow-monitor;",
    autocommit=True,
)


def app_sessions():
    """(session_id, program_name) of every user session except the monitor."""
    cur = MON.cursor()
    cur.execute(
        """
        SELECT session_id, program_name FROM sys.dm_exec_sessions
        WHERE is_user_process = 1 AND login_name = ?
          AND program_name <> 'bow-monitor'
        """,
        USER,
    )
    return [(r[0], r[1]) for r in cur.fetchall()]


def wait_sessions(n, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = app_sessions()
        if len(s) == n:
            return s
        time.sleep(0.3)
    return app_sessions()


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}{'  — ' + str(detail) if detail else ''}")
    if not ok:
        sys.exit(1)


def main():
    client = MSSQLClient(host=HOST, port=PORT, database=DB, user=USER, password=PWD)

    # --- 1. Health probe leaves nothing behind ---------------------------
    res = asyncio.run(client.atest_connection())
    check("probe succeeds", res.get("success") is True, res.get("message"))
    s = wait_sessions(0)
    check("probe leaves 0 server sessions", len(s) == 0, s)
    check("probe did not populate engine cache", engine_pool.stats()["cached_engines"] == 0)

    # --- 2. Normal query pools exactly one connection --------------------
    df = client.execute_query("SELECT 1 AS x")
    check("query works", int(df.iloc[0]["x"]) == 1)
    s = wait_sessions(1)
    check("1 pooled session stays open after query", len(s) == 1, s)
    pooled_sid = s[0][0]

    # --- 3. Steady traffic reuses the same session (LIFO) ----------------
    for _ in range(5):
        client.execute_query("SELECT 1 AS x")
    s = wait_sessions(1)
    check("5 more queries still 1 session", len(s) == 1, s)
    check("same physical session reused", s[0][0] == pooled_sid, f"{pooled_sid} -> {s[0][0]}")

    # --- 4. Idle reaper closes it server-side ----------------------------
    pruned = engine_pool.prune_idle_engines(max_idle_s=0)
    check("prune disposed 1 engine's pool", pruned == 1, pruned)
    s = wait_sessions(0)
    check("0 sessions after prune", len(s) == 0, s)

    # --- 5. Engine survives pruning --------------------------------------
    df = client.execute_query("SELECT 2 AS x")
    check("query after prune works (reconnects)", int(df.iloc[0]["x"]) == 2)
    s = wait_sessions(1)
    check("back to exactly 1 pooled session", len(s) == 1, s)

    # --- 6. A fresh TTL leaves an active pool alone ----------------------
    check("recent engine not pruned at 1h TTL", engine_pool.prune_idle_engines(max_idle_s=3600) == 0)
    s = wait_sessions(1)
    check("session still there", len(s) == 1, s)

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()

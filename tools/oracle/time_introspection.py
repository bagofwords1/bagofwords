#!/usr/bin/env python3
"""Time each introspection step the UI "Test connection" runs for Oracle.

Run inside the app container from /app/backend so the app's own client is used:

    cd /app/backend && ORACLE_HOST=... ORACLE_SERVICE_NAME=... ORACLE_USER=... \
        ORACLE_PASSWORD=... [ORACLE_SCHEMA=...] python /app/time_introspection.py

Each step is bounded by STEP_TIMEOUT seconds (default 120) via the Oracle
call timeout, so a slow dictionary view reports a timeout instead of hanging.
"""
import os
import sys
import time

import sqlalchemy
from sqlalchemy import text

from app.data_sources.clients.oracledb_client import OracledbClient, init_thick_mode_if_available
from app.data_sources.engine_pool import get_engine
from app.data_sources.fk_reflection import attach_foreign_keys

STEP_TIMEOUT = int(os.getenv("STEP_TIMEOUT", "120"))

print("thick mode:", init_thick_mode_if_available())
client = OracledbClient(
    host=os.environ["ORACLE_HOST"],
    port=int(os.getenv("ORACLE_PORT", "1521")),
    service_name=os.environ["ORACLE_SERVICE_NAME"],
    user=os.environ["ORACLE_USER"],
    password=os.environ["ORACLE_PASSWORD"],
    schema=os.getenv("ORACLE_SCHEMA") or None,
)
owner = (client._schemas[0] if client._schemas else client.user.upper())
print(f"owner filter used by the app: {owner}  (schema config: {client.schema!r})")

engine = get_engine(client.oracle_uri, connect_args=client._connect_args())


def timed(label, fn):
    t = time.perf_counter()
    try:
        out = fn()
        print(f"[OK] {label}: {out}  ({time.perf_counter() - t:.1f}s)")
        return out
    except Exception as e:
        print(f"[!!] {label}: {type(e).__name__}: {str(e).splitlines()[0]}  ({time.perf_counter() - t:.1f}s)")
        return None


with engine.connect() as conn:
    raw = conn.connection.dbapi_connection
    raw.call_timeout = STEP_TIMEOUT * 1000  # ms; Oracle aborts the statement on expiry

    timed("server version", lambda: raw.version)
    timed("tables per owner (all_tables, top 10)", lambda: conn.execute(text(
        "SELECT * FROM (SELECT owner, COUNT(*) n FROM all_tables GROUP BY owner ORDER BY n DESC) WHERE ROWNUM <= 10"
    )).fetchall())
    timed(f"column count for {owner} (all_tab_columns, no joins)", lambda: conn.execute(text(
        "SELECT COUNT(*) FROM all_tab_columns WHERE owner = :o"), {"o": owner}).scalar())
    timed(f"enriched query (columns + comments joins) for {owner}", lambda: len(conn.execute(text("""
        SELECT c.owner, c.table_name, c.column_name, c.data_type, cc.comments, tc.comments
        FROM all_tab_columns c
        LEFT JOIN all_col_comments cc ON c.owner = cc.owner AND c.table_name = cc.table_name AND c.column_name = cc.column_name
        LEFT JOIN all_tab_comments tc ON c.owner = tc.owner AND c.table_name = tc.table_name
        WHERE c.owner = :o ORDER BY c.owner, c.table_name, c.column_id"""), {"o": owner}).fetchall()))

    def fk():
        insp = sqlalchemy.inspect(conn)
        return sum(len(v) for v in insp.get_multi_foreign_keys(schema=owner).values())
    timed(f"FK reflection (all_constraints x all_cons_columns) for {owner}", fk)

print("done")

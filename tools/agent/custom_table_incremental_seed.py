#!/usr/bin/env python3
"""Seed a deliberately messy `shop` dataset into Postgres and/or SQL Server.

Used by the custom-table incremental feedback loop
(docs/feedback-loops/custom-table-incremental-refresh.md). The data is built
to look like a system that has been running for years rather than a demo:

* ids have gaps (rows deleted long ago) and the newest rows are not the
  highest ids
* `updated_at` is missing on some rows, shared by thousands of others (a bulk
  import stamped one second), sits in the future on one, and at the epoch on
  a few
* most rows are recent: two years of history with the bulk in the last months
* a notes column that is empty for the first ~100k rows, amounts that are
  negative, NULL or very large, currency codes in mixed case with stray
  spaces, names with quotes, emoji and right-to-left text
* JSON whose shape differs row to row (object, array, scalar, missing)
* an events table with no key and exact duplicate rows

    INCR_LOOP_PG_URL=postgresql://postgres:pw@localhost:55432/postgres \\
    INCR_LOOP_MSSQL_URL='mssql+pyodbc://sa:<pw>@localhost:51433/master?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes' \\
    uv run python ../tools/agent/custom_table_incremental_seed.py [--rows 600000]

Only ever point it at a disposable server: it drops and recreates `shop`.
"""
import argparse
import os
import sys
import time

from sqlalchemy import create_engine, text

PG_SCHEMA = """
DROP SCHEMA IF EXISTS shop CASCADE;
CREATE SCHEMA shop;
CREATE TABLE shop.orders (
    id            BIGINT PRIMARY KEY,
    customer_id   INTEGER,
    customer_name TEXT,
    amount        NUMERIC(14,2),
    currency      TEXT,
    status        TEXT,
    notes         TEXT,
    payload       JSONB,
    tags          TEXT[],
    ref           UUID,
    created_at    TIMESTAMPTZ,
    updated_at    TIMESTAMP
);
CREATE TABLE shop.events (
    happened_at TIMESTAMP,
    kind        TEXT,
    order_id    BIGINT,
    value       DOUBLE PRECISION
);
"""

# One INSERT … SELECT over generate_series. `r` is a per-row pseudo-random in
# [0, 1) derived from the id, so a re-seed produces the same data.
PG_ORDERS = """
INSERT INTO shop.orders
SELECT
    g AS id,
    CASE WHEN g % 53 = 0 THEN NULL ELSE (g * 7919) % 4000 END,
    CASE (g % 11)
        WHEN 0 THEN 'O''Brien & Sons'
        WHEN 1 THEN 'Müller GmbH'
        WHEN 2 THEN '株式会社テスト'
        WHEN 3 THEN 'שלום בע"מ'
        WHEN 4 THEN 'Acme 🚀 Rockets'
        WHEN 5 THEN ''
        WHEN 6 THEN NULL
        WHEN 7 THEN '  padded  '
        ELSE 'Customer ' || ((g * 31) % 9000)
    END,
    CASE
        WHEN g % 97 = 0 THEN NULL
        WHEN g % 41 = 0 THEN -round((r * 500)::numeric, 2)
        WHEN g % 5003 = 0 THEN 987654321098.76
        ELSE round((r * 1200)::numeric, 2)
    END,
    (ARRAY['USD','usd','EUR',' eur ','GBP',NULL,'ILS'])[1 + (g % 7)],
    (ARRAY['paid','PAID','refunded','pending','cancelled',NULL])[1 + (g % 6)],
    CASE WHEN g > :sparse_after AND g % 9 = 0 THEN 'note for order ' || g || repeat(' …', (g % 5)::int) END,
    CASE (g % 6)
        WHEN 0 THEN jsonb_build_object('source', 'web', 'items', (g % 4) + 1)
        WHEN 1 THEN jsonb_build_object('source', 'pos', 'store', jsonb_build_object('id', g % 30, 'city', 'Tel Aviv'))
        WHEN 2 THEN jsonb_build_array(g % 10, 'x')
        WHEN 3 THEN to_jsonb('legacy-import'::text)
        ELSE NULL
    END,
    CASE WHEN g % 4 = 0 THEN ARRAY['vip', 'b2b'] WHEN g % 4 = 1 THEN ARRAY[]::text[] END,
    md5(g::text)::uuid,
    ts AT TIME ZONE 'UTC',
    CASE
        WHEN g % 211 = 0 THEN NULL                                  -- never stamped
        WHEN g % 100003 = 0 THEN TIMESTAMP '1970-01-01 00:00:00'    -- epoch default
        WHEN g BETWEEN :bulk_lo AND :bulk_hi THEN TIMESTAMP '2026-03-01 02:00:00'  -- bulk import
        ELSE ts + (r * interval '6 hours')
    END
FROM (
    SELECT g,
           ((g * 2654435761) % 1000003)::float8 / 1000003 AS r,
           -- Skewed: a cubic curve packs most rows into the last months.
           TIMESTAMP '2024-11-01' + (interval '1 second' * (63072000 * (1 - power(1 - g::float8 / :rows, 3))))::interval AS ts
    FROM generate_series(1::bigint, :rows) g
    WHERE g % 17 <> 0            -- gaps: every 17th id was deleted long ago
) s;
"""

PG_EXTRAS = """
-- One row dated far in the future: a classic bad clock / typo.
UPDATE shop.orders SET updated_at = TIMESTAMP '2099-01-01 00:00:00'
WHERE id = (SELECT max(id) FROM shop.orders WHERE id % 1000 = 1);
CREATE INDEX orders_updated_at ON shop.orders (updated_at);

INSERT INTO shop.events
SELECT TIMESTAMP '2026-01-01' + (g % 200000) * interval '90 seconds',
       (ARRAY['view','click','purchase','refund'])[1 + (g % 4)],
       CASE WHEN g % 13 = 0 THEN NULL ELSE g % 50000 END,
       CASE WHEN g % 29 = 0 THEN NULL ELSE (g % 1000) / 7.0 END
FROM generate_series(1::bigint, :events) g;
-- Exact duplicates: the same event delivered twice.
INSERT INTO shop.events SELECT * FROM shop.events WHERE order_id % 500 = 0;
CREATE INDEX events_happened_at ON shop.events (happened_at);
ANALYZE shop.orders;
ANALYZE shop.events;
"""

MSSQL_SCHEMA = [
    "IF DB_ID('shop') IS NULL CREATE DATABASE shop",
]
MSSQL_TABLES = [
    "IF OBJECT_ID('dbo.orders') IS NOT NULL DROP TABLE dbo.orders",
    "IF OBJECT_ID('dbo.events') IS NOT NULL DROP TABLE dbo.events",
    """CREATE TABLE dbo.orders (
        id            BIGINT NOT NULL PRIMARY KEY,
        customer_id   INT NULL,
        customer_name NVARCHAR(200) NULL,
        amount        DECIMAL(14,2) NULL,
        currency      NVARCHAR(10) NULL,
        status        VARCHAR(20) NULL,
        notes         NVARCHAR(MAX) NULL,
        payload       NVARCHAR(MAX) NULL,
        is_gift       BIT NULL,
        ref           UNIQUEIDENTIFIER NULL,
        legacy_at     DATETIME NULL,
        updated_at    DATETIME2(7) NULL,
        rv            ROWVERSION
    )""",
    """CREATE TABLE dbo.events (
        happened_at DATETIME2(3) NULL,
        kind        VARCHAR(20) NULL,
        order_id    BIGINT NULL,
        value       FLOAT NULL
    )""",
]

MSSQL_ORDERS = """
WITH n AS (
    SELECT TOP (:rows) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS g
    FROM sys.all_objects a CROSS JOIN sys.all_objects b CROSS JOIN sys.all_objects c
), s AS (
    SELECT g,
           CAST((g * CAST(2654435761 AS BIGINT)) % 1000003 AS FLOAT) / 1000003 AS r,
           DATEADD(SECOND, CAST(63072000 * (1 - POWER(1 - CAST(g AS FLOAT) / :rows, 3)) AS INT),
                   CAST('2024-11-01' AS DATETIME2(7))) AS ts
    FROM n WHERE g % 17 <> 0
)
INSERT INTO dbo.orders (id, customer_id, customer_name, amount, currency, status, notes, payload,
                        is_gift, ref, legacy_at, updated_at)
SELECT
    g,
    CASE WHEN g % 53 = 0 THEN NULL ELSE (g * 7919) % 4000 END,
    CASE g % 11
        WHEN 0 THEN N'O''Brien & Sons'
        WHEN 1 THEN N'Müller GmbH'
        WHEN 2 THEN N'株式会社テスト'
        WHEN 3 THEN N'שלום בע"מ'
        WHEN 4 THEN N'Acme 🚀 Rockets'
        WHEN 5 THEN N''
        WHEN 6 THEN NULL
        WHEN 7 THEN N'  padded  '
        ELSE CONCAT(N'Customer ', (g * 31) % 9000)
    END,
    CASE
        WHEN g % 97 = 0 THEN NULL
        WHEN g % 41 = 0 THEN -ROUND(r * 500, 2)
        WHEN g % 5003 = 0 THEN 987654321098.76
        ELSE ROUND(r * 1200, 2)
    END,
    CHOOSE(1 + g % 7, N'USD', N'usd', N'EUR', N' eur ', N'GBP', NULL, N'ILS'),
    CHOOSE(1 + g % 6, 'paid', 'PAID', 'refunded', 'pending', 'cancelled', NULL),
    CASE WHEN g > :sparse_after AND g % 9 = 0 THEN CONCAT(N'note for order ', g) END,
    CASE g % 6
        WHEN 0 THEN CONCAT('{"source":"web","items":', (g % 4) + 1, '}')
        WHEN 1 THEN CONCAT('{"source":"pos","store":{"id":', g % 30, ',"city":"Tel Aviv"}}')
        WHEN 2 THEN CONCAT('[', g % 10, ',"x"]')
        WHEN 3 THEN 'not json at all'
        ELSE NULL
    END,
    CASE WHEN g % 3 = 0 THEN NULL WHEN g % 3 = 1 THEN 1 ELSE 0 END,
    CASE WHEN g % 23 = 0 THEN NULL ELSE NEWID() END,
    CAST(ts AS DATETIME),
    CASE
        WHEN g % 211 = 0 THEN NULL
        WHEN g % 100003 = 0 THEN CAST('1970-01-01' AS DATETIME2(7))
        WHEN g BETWEEN :bulk_lo AND :bulk_hi THEN CAST('2026-03-01 02:00:00' AS DATETIME2(7))
        -- 100 ns precision: more than a microsecond clock can hold.
        ELSE DATEADD(NANOSECOND, (g % 10) * 100, DATEADD(SECOND, CAST(r * 21600 AS INT), ts))
    END
FROM s
"""

MSSQL_EXTRAS = [
    """UPDATE dbo.orders SET updated_at = CAST('2099-01-01' AS DATETIME2(7))
       WHERE id = (SELECT MAX(id) FROM dbo.orders WHERE id % 1000 = 1)""",
    "CREATE INDEX orders_updated_at ON dbo.orders (updated_at)",
    """WITH n AS (
        SELECT TOP (:events) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS g
        FROM sys.all_objects a CROSS JOIN sys.all_objects b CROSS JOIN sys.all_objects c
    )
    INSERT INTO dbo.events
    SELECT DATEADD(SECOND, (g % 200000) * 90, CAST('2026-01-01' AS DATETIME2(3))),
           CHOOSE(1 + g % 4, 'view', 'click', 'purchase', 'refund'),
           CASE WHEN g % 13 = 0 THEN NULL ELSE g % 50000 END,
           CASE WHEN g % 29 = 0 THEN NULL ELSE (g % 1000) / 7.0 END
    FROM n""",
    "INSERT INTO dbo.events SELECT * FROM dbo.events WHERE order_id % 500 = 0",
    "CREATE INDEX events_happened_at ON dbo.events (happened_at)",
]


def _params(rows: int) -> dict:
    return {
        "rows": rows,
        "events": max(1000, rows // 4),
        # The notes column stays empty for the first sixth of the table.
        "sparse_after": rows // 6,
        # ~1% of the table stamped with the same second.
        "bulk_lo": rows // 2,
        "bulk_hi": rows // 2 + rows // 100,
    }


def seed_postgres(url: str, rows: int) -> dict:
    engine = create_engine(url)
    params = _params(rows)
    started = time.time()
    with engine.begin() as conn:
        for statement in PG_SCHEMA.split(";"):
            if statement.strip():
                conn.execute(text(statement))
        conn.execute(text(PG_ORDERS), params)
        for statement in PG_EXTRAS.split(";"):
            if statement.strip():
                conn.execute(text(statement), params if ":events" in statement else {})
    with engine.connect() as conn:
        orders = conn.execute(text("SELECT count(*), count(updated_at) FROM shop.orders")).one()
        events = conn.execute(text("SELECT count(*) FROM shop.events")).scalar()
    engine.dispose()
    return {"engine": "postgres", "orders": orders[0], "orders_with_updated_at": orders[1],
            "events": events, "seconds": round(time.time() - started, 1)}


def mssql_database_url(url: str, database: str = "shop") -> str:
    from sqlalchemy.engine import make_url

    return make_url(url).set(database=database).render_as_string(hide_password=False)


def seed_mssql(url: str, rows: int) -> dict:
    params = _params(rows)
    started = time.time()
    master = create_engine(url, isolation_level="AUTOCOMMIT")
    with master.connect() as conn:
        for statement in MSSQL_SCHEMA:
            conn.execute(text(statement))
    master.dispose()
    engine = create_engine(mssql_database_url(url), isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        for statement in MSSQL_TABLES:
            conn.execute(text(statement))
        conn.execute(text(MSSQL_ORDERS), params)
        for statement in MSSQL_EXTRAS:
            conn.execute(text(statement), params if ":events" in statement else {})
        orders = conn.execute(text("SELECT COUNT(*), COUNT(updated_at) FROM dbo.orders")).one()
        events = conn.execute(text("SELECT COUNT(*) FROM dbo.events")).scalar()
    engine.dispose()
    return {"engine": "mssql", "orders": orders[0], "orders_with_updated_at": orders[1],
            "events": events, "seconds": round(time.time() - started, 1)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", type=int, default=600_000)
    args = parser.parse_args()
    pg, mssql = os.environ.get("INCR_LOOP_PG_URL"), os.environ.get("INCR_LOOP_MSSQL_URL")
    if not pg and not mssql:
        print("Set INCR_LOOP_PG_URL and/or INCR_LOOP_MSSQL_URL.", file=sys.stderr)
        return 2
    if pg:
        print(seed_postgres(pg, args.rows))
    if mssql:
        print(seed_mssql(mssql, args.rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())

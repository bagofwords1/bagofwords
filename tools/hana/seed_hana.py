"""Seed the local SAP HANA Express (tools/hana/docker-compose.yaml) with demo
data for the sap_hana connector feedback loop.

Creates a BOW_DEMO schema with two tables, a view (mirrors how SAP Datasphere
exposes data — consumers query views, not tables), comments, and a few rows.

Usage: python tools/hana/seed_hana.py  (pip install hdbcli)
Env overrides: HANA_HOST, HANA_PORT, HANA_USER, HANA_PASSWORD
"""
import os

from hdbcli import dbapi

HOST = os.getenv("HANA_HOST", "localhost")
PORT = int(os.getenv("HANA_PORT", "39041"))
USER = os.getenv("HANA_USER", "SYSTEM")
PASSWORD = os.getenv("HANA_PASSWORD", "HXEHana1")

STATEMENTS = [
    'CREATE SCHEMA BOW_DEMO',
    '''CREATE COLUMN TABLE BOW_DEMO.CUSTOMERS (
        ID INTEGER PRIMARY KEY,
        NAME NVARCHAR(100),
        COUNTRY NVARCHAR(50),
        CREATED_AT DATE
    )''',
    "COMMENT ON TABLE BOW_DEMO.CUSTOMERS IS 'Demo customers'",
    "COMMENT ON COLUMN BOW_DEMO.CUSTOMERS.COUNTRY IS 'ISO country name'",
    '''CREATE COLUMN TABLE BOW_DEMO.ORDERS (
        ID INTEGER PRIMARY KEY,
        CUSTOMER_ID INTEGER,
        AMOUNT DECIMAL(10,2),
        ORDER_DATE DATE
    )''',
    """INSERT INTO BOW_DEMO.CUSTOMERS VALUES (1, 'Acme GmbH', 'Germany', '2025-01-15')""",
    """INSERT INTO BOW_DEMO.CUSTOMERS VALUES (2, 'Globex Ltd', 'United Kingdom', '2025-03-02')""",
    """INSERT INTO BOW_DEMO.CUSTOMERS VALUES (3, 'Initech Inc', 'United States', '2025-06-20')""",
    """INSERT INTO BOW_DEMO.ORDERS VALUES (1, 1, 1200.50, '2026-01-10')""",
    """INSERT INTO BOW_DEMO.ORDERS VALUES (2, 1, 340.00, '2026-02-11')""",
    """INSERT INTO BOW_DEMO.ORDERS VALUES (3, 2, 89.99, '2026-02-28')""",
    """INSERT INTO BOW_DEMO.ORDERS VALUES (4, 3, 5600.00, '2026-05-05')""",
    '''CREATE VIEW BOW_DEMO.V_REVENUE_BY_COUNTRY AS
        SELECT c.COUNTRY, SUM(o.AMOUNT) AS REVENUE, COUNT(*) AS ORDER_COUNT
        FROM BOW_DEMO.ORDERS o
        JOIN BOW_DEMO.CUSTOMERS c ON c.ID = o.CUSTOMER_ID
        GROUP BY c.COUNTRY''',
]


def main() -> None:
    conn = dbapi.connect(address=HOST, port=PORT, user=USER, password=PASSWORD)
    cursor = conn.cursor()
    for stmt in STATEMENTS:
        try:
            cursor.execute(stmt)
            print(f"ok:   {stmt.split(chr(10))[0][:70]}")
        except dbapi.Error as e:
            # Idempotent re-runs: skip "already exists" / duplicate key errors.
            print(f"skip: {stmt.split(chr(10))[0][:70]} ({e})")
    cursor.close()
    conn.close()
    print("done")


if __name__ == "__main__":
    main()

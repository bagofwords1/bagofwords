"""Simulate the three Db2 products on one Db2 LUW test server.

Only Db2 LUW runs in a container. Db2 for z/OS and Db2 for IBM i expose the
same SQL dialect through different system catalogs, so this module builds
stand-in views with their catalog's names and column layout over SYSCAT:

* ``ZOSSIM.SYSTABLES / SYSCOLUMNS / SYSRELS / SYSFOREIGNKEYS`` mimic
  ``SYSIBM.*`` on z/OS (SYSIBM cannot hold user objects on LUW, so the z/OS
  client is pointed at ZOSSIM via ``Db2Client.catalog_schemas``).
* ``QSYS2.SYSTABLES / SYSCOLUMNS / SYSCST / SYSKEYCST / SYSREFCST`` mimic the
  IBM i catalog under its real schema name, so IBM i clients need no override.

It also seeds a small, commented ``SALES`` schema (customers, orders, order
lines, products) used by the integration tests and the sandbox feedback loop.

What this does NOT simulate: Db2 Connect licensing, EBCDIC sort order,
CCSID 65535 columns, IBM i system naming (LIB/FILE), or real z/OS catalog
quirks beyond the columns the connector reads.

Run directly to seed a running container:
    python tests/integrations/db2_sim.py "DATABASE=TESTDB;HOSTNAME=127.0.0.1;PORT=50000;PROTOCOL=TCPIP;UID=db2inst1;PWD=...;"
"""

from __future__ import annotations

import sys

ZOS_SIM_SCHEMA = "ZOSSIM"

SALES_DDL = [
    "CREATE TABLE SALES.CUSTOMERS (CUSTOMER_ID INTEGER NOT NULL PRIMARY KEY, "
    "NAME VARCHAR(80) NOT NULL, REGION CHAR(4) NOT NULL, SEGMENT VARCHAR(20), "
    "CREATED_AT DATE NOT NULL)",
    "CREATE TABLE SALES.PRODUCTS (PRODUCT_ID INTEGER NOT NULL PRIMARY KEY, "
    "NAME VARCHAR(80) NOT NULL, CATEGORY VARCHAR(30) NOT NULL, LIST_PRICE DECIMAL(9,2) NOT NULL)",
    "CREATE TABLE SALES.ORDERS (ORDER_ID INTEGER NOT NULL PRIMARY KEY, "
    "CUSTOMER_ID INTEGER NOT NULL, ORDER_DATE DATE NOT NULL, STATUS CHAR(1) NOT NULL, "
    "CONSTRAINT FK_ORD_CUST FOREIGN KEY (CUSTOMER_ID) REFERENCES SALES.CUSTOMERS (CUSTOMER_ID))",
    "CREATE TABLE SALES.ORDER_LINES (ORDER_ID INTEGER NOT NULL, LINE_NO SMALLINT NOT NULL, "
    "PRODUCT_ID INTEGER NOT NULL, QUANTITY INTEGER NOT NULL, UNIT_PRICE DECIMAL(9,2) NOT NULL, "
    "PRIMARY KEY (ORDER_ID, LINE_NO), "
    "CONSTRAINT FK_LINE_ORD FOREIGN KEY (ORDER_ID) REFERENCES SALES.ORDERS (ORDER_ID), "
    "CONSTRAINT FK_LINE_PROD FOREIGN KEY (PRODUCT_ID) REFERENCES SALES.PRODUCTS (PRODUCT_ID))",
    "COMMENT ON TABLE SALES.CUSTOMERS IS 'Customer master, one row per account'",
    "COMMENT ON TABLE SALES.ORDERS IS 'Order headers'",
    "COMMENT ON TABLE SALES.ORDER_LINES IS 'Order line items; revenue = QUANTITY * UNIT_PRICE'",
    "COMMENT ON COLUMN SALES.ORDERS.STATUS IS 'O=open, S=shipped, C=cancelled'",
    "COMMENT ON COLUMN SALES.CUSTOMERS.REGION IS 'Sales region code: EMEA, AMER, APAC'",
]

_REGIONS = ["EMEA", "AMER", "APAC"]
_SEGMENTS = ["Enterprise", "Mid-market", "SMB"]
_PRODUCTS = [
    (1, "Mainframe Monitor", "Software", 1200.00),
    (2, "Batch Scheduler", "Software", 800.00),
    (3, "Support Plan", "Services", 300.00),
    (4, "Training Day", "Services", 950.00),
    (5, "Storage Module", "Hardware", 2500.00),
]


def _sales_rows():
    customers = [
        (i, f"Customer {i:02d}", _REGIONS[i % 3], _SEGMENTS[i % 3], f"2025-{(i % 12) + 1:02d}-01")
        for i in range(1, 25)
    ]
    orders, lines = [], []
    oid = 0
    for month in range(1, 13):
        for c in range(1, 25, 2 + month % 3):
            oid += 1
            status = "C" if oid % 11 == 0 else ("O" if month == 12 else "S")
            orders.append((oid, c, f"2026-{month:02d}-{(oid % 27) + 1:02d}", status))
            for ln in range(1, 1 + (oid % 3) + 1):
                p = _PRODUCTS[(oid + ln) % len(_PRODUCTS)]
                lines.append((oid, ln, p[0], 1 + (oid * ln) % 4, p[3]))
    return customers, orders, lines


ZOS_VIEWS = [
    f"CREATE OR REPLACE VIEW {ZOS_SIM_SCHEMA}.SYSTABLES (CREATOR, NAME, TYPE, REMARKS) AS "
    "SELECT TABSCHEMA, TABNAME, CASE TYPE WHEN 'S' THEN 'M' ELSE TYPE END, REMARKS FROM SYSCAT.TABLES",
    f"CREATE OR REPLACE VIEW {ZOS_SIM_SCHEMA}.SYSCOLUMNS "
    "(TBCREATOR, TBNAME, NAME, COLTYPE, LENGTH, SCALE, NULLS, REMARKS, COLNO, KEYSEQ) AS "
    "SELECT TBCREATOR, TBNAME, NAME, COLTYPE, LENGTH, SCALE, NULLS, REMARKS, COLNO, "
    "COALESCE(KEYSEQ, 0) FROM SYSIBM.SYSCOLUMNS",
    f"CREATE OR REPLACE VIEW {ZOS_SIM_SCHEMA}.SYSRELS (CREATOR, TBNAME, RELNAME, REFTBCREATOR, REFTBNAME) AS "
    "SELECT TABSCHEMA, TABNAME, CONSTNAME, REFTABSCHEMA, REFTABNAME FROM SYSCAT.REFERENCES",
    f"CREATE OR REPLACE VIEW {ZOS_SIM_SCHEMA}.SYSFOREIGNKEYS (CREATOR, TBNAME, RELNAME, COLNAME, COLSEQ) AS "
    "SELECT k.TABSCHEMA, k.TABNAME, k.CONSTNAME, k.COLNAME, k.COLSEQ FROM SYSCAT.KEYCOLUSE k "
    "JOIN SYSCAT.REFERENCES r ON r.CONSTNAME = k.CONSTNAME AND r.TABSCHEMA = k.TABSCHEMA "
    "AND r.TABNAME = k.TABNAME",
]

IBMI_VIEWS = [
    # IBM i reports SQL tables as 'T' and DDS physical files as 'P'; LUW
    # tables become 'T', so both code paths of the kind map are exercised by
    # unit tests rather than here.
    "CREATE OR REPLACE VIEW QSYS2.SYSTABLES "
    "(TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, TABLE_TEXT, LONG_COMMENT) AS "
    "SELECT TABSCHEMA, TABNAME, CASE TYPE WHEN 'S' THEN 'M' ELSE TYPE END, "
    "CAST(LEFT(COALESCE(REMARKS, ''), 50) AS VARCHAR(50)), REMARKS FROM SYSCAT.TABLES",
    "CREATE OR REPLACE VIEW QSYS2.SYSCOLUMNS (TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, "
    "LENGTH, NUMERIC_SCALE, IS_NULLABLE, COLUMN_TEXT, LONG_COMMENT, ORDINAL_POSITION) AS "
    "SELECT TABSCHEMA, TABNAME, COLNAME, TYPENAME, LENGTH, SCALE, NULLS, "
    "CAST(NULL AS VARCHAR(50)), REMARKS, COLNO + 1 FROM SYSCAT.COLUMNS",
    "CREATE OR REPLACE VIEW QSYS2.SYSCST (CONSTRAINT_SCHEMA, CONSTRAINT_NAME, CONSTRAINT_TYPE, "
    "TABLE_SCHEMA, TABLE_NAME) AS "
    "SELECT TABSCHEMA, CONSTNAME, CASE TYPE WHEN 'P' THEN 'PRIMARY KEY' WHEN 'F' THEN 'FOREIGN KEY' "
    "WHEN 'U' THEN 'UNIQUE' ELSE 'CHECK' END, TABSCHEMA, TABNAME FROM SYSCAT.TABCONST",
    "CREATE OR REPLACE VIEW QSYS2.SYSKEYCST (CONSTRAINT_SCHEMA, CONSTRAINT_NAME, TABLE_SCHEMA, "
    "TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION) AS "
    "SELECT TABSCHEMA, CONSTNAME, TABSCHEMA, TABNAME, COLNAME, COLSEQ FROM SYSCAT.KEYCOLUSE",
    "CREATE OR REPLACE VIEW QSYS2.SYSREFCST (CONSTRAINT_SCHEMA, CONSTRAINT_NAME, "
    "UNIQUE_CONSTRAINT_SCHEMA, UNIQUE_CONSTRAINT_NAME) AS "
    "SELECT TABSCHEMA, CONSTNAME, REFTABSCHEMA, REFKEYNAME FROM SYSCAT.REFERENCES",
]


def seed(dsn: str) -> None:
    """Create SALES (if missing) and the z/OS + IBM i catalog stand-ins. Idempotent."""
    import ibm_db_dbi

    conn = ibm_db_dbi.connect(dsn, "", "")
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM SYSCAT.TABLES WHERE TABSCHEMA = 'SALES'")
    if cur.fetchone()[0] == 0:
        for stmt in SALES_DDL:
            cur.execute(stmt)
        customers, orders, lines = _sales_rows()
        cur.executemany("INSERT INTO SALES.PRODUCTS VALUES (?, ?, ?, ?)", _PRODUCTS)
        cur.executemany("INSERT INTO SALES.CUSTOMERS VALUES (?, ?, ?, ?, ?)", customers)
        cur.executemany("INSERT INTO SALES.ORDERS VALUES (?, ?, ?, ?)", orders)
        cur.executemany("INSERT INTO SALES.ORDER_LINES VALUES (?, ?, ?, ?, ?)", lines)
    for stmt in ZOS_VIEWS + IBMI_VIEWS:
        cur.execute(stmt)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    seed(sys.argv[1])
    print("seeded")

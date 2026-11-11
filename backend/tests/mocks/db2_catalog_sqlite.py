"""sqlite stand-ins for the three Db2 system catalogs, for unit tests.

`build(dir, platform)` writes sqlite files laid out like Db2's catalog for one
product — ``SYSCAT.*`` (LUW), ``SYSIBM.SYS*`` (z/OS) or ``QSYS2.SYS*`` (IBM i) —
plus the user tables themselves, all describing the same small model:

* ``SALES.CUSTOMERS`` (PK CUSTOMER_ID, commented)
* ``SALES.ORDERS`` (PK ORDER_ID, FK CUSTOMER_ID -> SALES.CUSTOMERS)
* ``SALES.V_BIG_ORDERS`` (view)
* ``OPS.JOBS`` (PK JOB_ID)
* one table in a system schema of that product, which discovery must hide.

`connect(dir)` opens it as one DB-API connection with every schema attached,
so the connector's real catalog SQL runs unchanged (qmark params, JOINs,
COALESCE, NOT LIKE).
"""

from __future__ import annotations

import os
import sqlite3

SYSTEM_TABLE = {"luw": ("SYSTOOLS", "POLICY"), "zos": ("DSNACC", "CANDIDATES"), "ibmi": ("QGPL", "QAUDIT")}

# (schema, table, kind, comment, columns[(name, type, length, scale, nullable, comment)], pk[cols])
MODEL = [
    ("SALES", "CUSTOMERS", "T", "Customer master", [
        ("CUSTOMER_ID", "INTEGER", 4, 0, "N", None),
        ("NAME", "VARCHAR", 80, 0, "N", None),
        ("REGION", "CHARACTER", 4, 0, "Y", "Sales region code"),
    ], ["CUSTOMER_ID"]),
    ("SALES", "ORDERS", "T", None, [
        ("ORDER_ID", "INTEGER", 4, 0, "N", None),
        ("CUSTOMER_ID", "INTEGER", 4, 0, "N", None),
        ("AMOUNT", "DECIMAL", 9, 2, "N", "Order total"),
    ], ["ORDER_ID"]),
    ("SALES", "V_BIG_ORDERS", "V", "Orders over 1000", [
        ("ORDER_ID", "INTEGER", 4, 0, "N", None),
        ("AMOUNT", "DECIMAL", 9, 2, "N", None),
    ], []),
    ("OPS", "JOBS", "T", None, [
        ("JOB_ID", "BIGINT", 8, 0, "N", None),
        ("STARTED_AT", "TIMESTAMP", 10, 6, "Y", None),
    ], ["JOB_ID"]),
]
# (schema, table, column, ref_schema, ref_table, ref_column, constraint)
FKS = [("SALES", "ORDERS", "CUSTOMER_ID", "SALES", "CUSTOMERS", "CUSTOMER_ID", "FK_ORD_CUST")]

DATA = {
    ("SALES", "CUSTOMERS"): [(1, "Acme", "EMEA"), (2, "Globex", "AMER"), (3, "Initech", "APAC")],
    ("SALES", "ORDERS"): [(10, 1, 1500.0), (11, 1, 200.0), (12, 2, 999.5), (13, 3, 4200.0), (14, 3, 75.0)],
}

CATALOG_SCHEMA = {"luw": "SYSCAT", "zos": "SYSIBM", "ibmi": "QSYS2"}


def _model(platform: str):
    sys_schema, sys_table = SYSTEM_TABLE[platform]
    return MODEL + [(sys_schema, sys_table, "T", None, [("ID", "INTEGER", 4, 0, "N", None)], ["ID"])]


def _schemas(platform: str) -> list[str]:
    return sorted({m[0] for m in _model(platform)} | {CATALOG_SCHEMA[platform], "SYSIBM"})


def connect(directory: str, platform: str) -> sqlite3.Connection:
    conn = sqlite3.connect(os.path.join(directory, "main.db"), check_same_thread=False)
    for schema in _schemas(platform):
        conn.execute(f"ATTACH DATABASE '{os.path.join(directory, schema + '.db')}' AS {schema}")
    return conn


def build(directory: str, platform: str) -> None:
    conn = connect(directory, platform)
    cat = CATALOG_SCHEMA[platform]
    model = _model(platform)
    ex = conn.execute
    # Db2's one-row table, present on every product.
    ex("CREATE TABLE SYSIBM.SYSDUMMY1 (IBMREQD)")
    ex("INSERT INTO SYSIBM.SYSDUMMY1 VALUES ('Y')")

    # User tables with data (views are plain tables here; only the catalog says "view").
    for schema, table, _kind, _c, cols, _pk in model:
        ex(f"CREATE TABLE {schema}.{table} ({', '.join(c[0] for c in cols)})")
        for row in DATA.get((schema, table), []):
            ex(f"INSERT INTO {schema}.{table} VALUES ({', '.join('?' * len(row))})", row)

    if platform == "luw":
        ex(f"CREATE TABLE {cat}.TABLES (TABSCHEMA, TABNAME, TYPE, REMARKS)")
        ex(f"CREATE TABLE {cat}.COLUMNS (TABSCHEMA, TABNAME, COLNAME, TYPENAME, LENGTH, SCALE, NULLS, REMARKS, COLNO)")
        ex(f"CREATE TABLE {cat}.TABCONST (CONSTNAME, TABSCHEMA, TABNAME, TYPE)")
        ex(f"CREATE TABLE {cat}.KEYCOLUSE (CONSTNAME, TABSCHEMA, TABNAME, COLNAME, COLSEQ)")
        ex(f'CREATE TABLE {cat}."REFERENCES" (CONSTNAME, TABSCHEMA, TABNAME, REFKEYNAME, REFTABSCHEMA, REFTABNAME)')
        for schema, table, kind, comment, cols, pk in model:
            ex(f"INSERT INTO {cat}.TABLES VALUES (?,?,?,?)", (schema, table, kind, comment))
            for i, (name, typ, length, scale, nulls, ccom) in enumerate(cols):
                ex(f"INSERT INTO {cat}.COLUMNS VALUES (?,?,?,?,?,?,?,?,?)",
                   (schema, table, name, typ, length, scale, nulls, ccom, i))
            if pk:
                ex(f"INSERT INTO {cat}.TABCONST VALUES (?,?,?,'P')", (f"PK_{table}", schema, table))
                for i, col in enumerate(pk, 1):
                    ex(f"INSERT INTO {cat}.KEYCOLUSE VALUES (?,?,?,?,?)", (f"PK_{table}", schema, table, col, i))
        for s, t, c, rs, rt, rc, name in FKS:
            ex(f"INSERT INTO {cat}.TABCONST VALUES (?,?,?,'F')", (name, s, t))
            ex(f"INSERT INTO {cat}.KEYCOLUSE VALUES (?,?,?,?,1)", (name, s, t, c))
            ex(f'INSERT INTO {cat}."REFERENCES" VALUES (?,?,?,?,?,?)', (name, s, t, f"PK_{rt}", rs, rt))

    elif platform == "zos":
        ex(f"CREATE TABLE {cat}.SYSTABLES (CREATOR, NAME, TYPE, REMARKS)")
        ex(f"CREATE TABLE {cat}.SYSCOLUMNS (TBCREATOR, TBNAME, NAME, COLTYPE, LENGTH, SCALE, NULLS, REMARKS, COLNO, KEYSEQ)")
        ex(f"CREATE TABLE {cat}.SYSRELS (CREATOR, TBNAME, RELNAME, REFTBCREATOR, REFTBNAME)")
        ex(f"CREATE TABLE {cat}.SYSFOREIGNKEYS (CREATOR, TBNAME, RELNAME, COLNAME, COLSEQ)")
        for schema, table, kind, comment, cols, pk in model:
            # z/OS pads COLTYPE (CHAR(8)) and reports blank REMARKS, not NULL.
            ex(f"INSERT INTO {cat}.SYSTABLES VALUES (?,?,?,?)", (schema, table, kind, comment or ""))
            for i, (name, typ, length, scale, nulls, ccom) in enumerate(cols, 1):
                keyseq = pk.index(name) + 1 if name in pk else 0
                ex(f"INSERT INTO {cat}.SYSCOLUMNS VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (schema, table, name, (typ[:8]).ljust(8), length, scale, nulls, ccom or "", i, keyseq))
        for s, t, c, rs, rt, rc, name in FKS:
            ex(f"INSERT INTO {cat}.SYSRELS VALUES (?,?,?,?,?)", (s, t, name, rs, rt))
            ex(f"INSERT INTO {cat}.SYSFOREIGNKEYS VALUES (?,?,?,?,1)", (s, t, name, c))

    else:  # ibmi
        ex(f"CREATE TABLE {cat}.SYSTABLES (TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, TABLE_TEXT, LONG_COMMENT)")
        ex(f"CREATE TABLE {cat}.SYSCOLUMNS (TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, LENGTH, "
           "NUMERIC_SCALE, IS_NULLABLE, COLUMN_TEXT, LONG_COMMENT, ORDINAL_POSITION)")
        ex(f"CREATE TABLE {cat}.SYSCST (CONSTRAINT_SCHEMA, CONSTRAINT_NAME, CONSTRAINT_TYPE, TABLE_SCHEMA, TABLE_NAME)")
        ex(f"CREATE TABLE {cat}.SYSKEYCST (CONSTRAINT_SCHEMA, CONSTRAINT_NAME, TABLE_SCHEMA, TABLE_NAME, "
           "COLUMN_NAME, ORDINAL_POSITION)")
        ex(f"CREATE TABLE {cat}.SYSREFCST (CONSTRAINT_SCHEMA, CONSTRAINT_NAME, UNIQUE_CONSTRAINT_SCHEMA, "
           "UNIQUE_CONSTRAINT_NAME)")
        for schema, table, kind, comment, cols, pk in model:
            # IBM i: SQL tables are 'T'; short text in TABLE_TEXT, long in LONG_COMMENT.
            ex(f"INSERT INTO {cat}.SYSTABLES VALUES (?,?,?,?,?)",
               (schema, table, "P" if schema == "OPS" else kind, comment, None))
            for i, (name, typ, length, scale, nulls, ccom) in enumerate(cols, 1):
                ex(f"INSERT INTO {cat}.SYSCOLUMNS VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (schema, table, name, typ, length, scale, "YES" if nulls == "Y" else "NO", ccom, None, i))
            if pk:
                ex(f"INSERT INTO {cat}.SYSCST VALUES (?,?,'PRIMARY KEY',?,?)", (schema, f"PK_{table}", schema, table))
                for i, col in enumerate(pk, 1):
                    ex(f"INSERT INTO {cat}.SYSKEYCST VALUES (?,?,?,?,?,?)", (schema, f"PK_{table}", schema, table, col, i))
        for s, t, c, rs, rt, rc, name in FKS:
            ex(f"INSERT INTO {cat}.SYSCST VALUES (?,?,'FOREIGN KEY',?,?)", (s, name, s, t))
            ex(f"INSERT INTO {cat}.SYSKEYCST VALUES (?,?,?,?,?,1)", (s, name, s, t, c))
            ex(f"INSERT INTO {cat}.SYSREFCST VALUES (?,?,?,?)", (s, name, rs, f"PK_{rt}"))
    conn.commit()
    conn.close()

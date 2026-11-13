"""Catalog discovery shared by the Db2 family of clients.

Db2 ships as three products with one SQL dialect but three different system
catalogs:

* **LUW** (Linux/Unix/Windows, Db2 on Cloud, Db2 Warehouse): ``SYSCAT.*``
* **z/OS** (mainframe): ``SYSIBM.SYS*``
* **IBM i** (AS/400, "Db2 for i"): ``QSYS2.SYS*``

Each product gets a :class:`CatalogQueries` with four statements (objects,
columns, primary keys, foreign keys) that all return the same column order, so
one mapper (:func:`build_tables`) turns the rows into ``Table`` objects no
matter which product or driver produced them. The Db2 client (``ibm_db``) and
the IBM i client (Mapepire) only differ in how they run a statement.

Queries take ``?`` placeholders; both drivers bind them positionally. Every
selected column has a distinct name: Mapepire can key rows by column name.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.ai.prompt_formatters import ForeignKey, Table, TableColumn
from app.data_sources.clients.progress import IndexingCancelled, discovery_items, discovery_phase

logger = logging.getLogger(__name__)

LUW = "luw"
ZOS = "zos"
IBMI = "ibmi"
PLATFORMS = (LUW, ZOS, IBMI)
PLATFORM_LABELS = {LUW: "Db2 LUW", ZOS: "Db2 for z/OS", IBMI: "Db2 for IBM i"}

# Runs a statement with positional params and returns a list of row tuples.
Runner = Callable[[str, Sequence], list]


def platform_from_dbms_name(dbms_name: str | None) -> str:
    """Map the driver's SQL_DBMS_NAME to a platform.

    Db2 reports ``DB2/<os>`` on LUW (e.g. ``DB2/LINUXX8664``, ``DB2/NT64``),
    plain ``DB2`` on z/OS, and ``AS`` on IBM i. Older z/OS levels report a
    ``DSN...`` product id. Anything unrecognised is treated as LUW, the most
    common deployment.
    """
    name = (dbms_name or "").strip().upper()
    if name == "AS" or name.startswith("AS/") or name.startswith("QSQ"):
        return IBMI
    if name == "DB2" or name.startswith("DSN") or "Z/OS" in name or "OS/390" in name:
        return ZOS
    return LUW


@dataclass(frozen=True)
class CatalogQueries:
    # Each returns: schema, name, kind, comment
    objects: str
    # Each returns: schema, table, column, type, length, scale, nullable(Y/N), comment, position
    columns: str
    # Each returns: schema, table, column  (ordered by key position)
    primary_keys: str
    # Each returns: schema, table, column, ref_schema, ref_table, ref_column  (ordered)
    foreign_keys: str
    schema_column_objects: str
    schema_column_columns: str
    schema_column_keys: str
    schema_column_fks: str
    # Schema names (exact) and prefixes (LIKE 'X%') hidden unless listed explicitly.
    system_schemas: tuple[str, ...]
    system_prefixes: tuple[str, ...]
    # Object kind code -> readable name; only these kinds are indexed.
    kinds: dict


def luw_queries(catalog: str = "SYSCAT") -> CatalogQueries:
    return CatalogQueries(
        objects=(
            f"SELECT TABSCHEMA, TABNAME, TYPE, REMARKS FROM {catalog}.TABLES "
            "WHERE TYPE IN ('T','V','S','N') AND {schema_filter} "
            "ORDER BY TABSCHEMA, TABNAME"
        ),
        columns=(
            f"SELECT c.TABSCHEMA, c.TABNAME, c.COLNAME, c.TYPENAME, c.LENGTH, c.SCALE, "
            f"c.NULLS, c.REMARKS, c.COLNO FROM {catalog}.COLUMNS c "
            f"JOIN {catalog}.TABLES t ON t.TABSCHEMA = c.TABSCHEMA AND t.TABNAME = c.TABNAME "
            "WHERE t.TYPE IN ('T','V','S','N') AND {schema_filter} "
            "ORDER BY c.TABSCHEMA, c.TABNAME, c.COLNO"
        ),
        primary_keys=(
            f"SELECT k.TABSCHEMA, k.TABNAME, k.COLNAME FROM {catalog}.TABCONST c "
            f"JOIN {catalog}.KEYCOLUSE k ON k.CONSTNAME = c.CONSTNAME "
            "AND k.TABSCHEMA = c.TABSCHEMA AND k.TABNAME = c.TABNAME "
            "WHERE c.TYPE = 'P' AND {schema_filter} "
            "ORDER BY k.TABSCHEMA, k.TABNAME, k.COLSEQ"
        ),
        foreign_keys=(
            f"SELECT r.TABSCHEMA, r.TABNAME, fk.COLNAME, r.REFTABSCHEMA, r.REFTABNAME, pk.COLNAME AS REFCOLNAME "
            f"FROM {catalog}.\"REFERENCES\" r "
            f"JOIN {catalog}.KEYCOLUSE fk ON fk.CONSTNAME = r.CONSTNAME "
            "AND fk.TABSCHEMA = r.TABSCHEMA AND fk.TABNAME = r.TABNAME "
            f"JOIN {catalog}.KEYCOLUSE pk ON pk.CONSTNAME = r.REFKEYNAME "
            "AND pk.TABSCHEMA = r.REFTABSCHEMA AND pk.TABNAME = r.REFTABNAME "
            "AND pk.COLSEQ = fk.COLSEQ "
            "WHERE {schema_filter} "
            "ORDER BY r.TABSCHEMA, r.TABNAME, r.CONSTNAME, fk.COLSEQ"
        ),
        schema_column_objects="TABSCHEMA",
        schema_column_columns="c.TABSCHEMA",
        schema_column_keys="k.TABSCHEMA",
        schema_column_fks="r.TABSCHEMA",
        system_schemas=("NULLID", "SQLJ"),
        system_prefixes=("SYS",),
        kinds={"T": "table", "V": "view", "S": "materialized_query_table", "N": "nickname"},
    )


def zos_queries(catalog: str = "SYSIBM") -> CatalogQueries:
    # SYSCOLUMNS.KEYSEQ is the column's position in the primary key (0 = not
    # part of it), so the PK needs no index/key join on z/OS.
    return CatalogQueries(
        objects=(
            f"SELECT CREATOR, NAME, TYPE, REMARKS FROM {catalog}.SYSTABLES "
            "WHERE TYPE IN ('T','V','M') AND {schema_filter} "
            "ORDER BY CREATOR, NAME"
        ),
        columns=(
            f"SELECT c.TBCREATOR, c.TBNAME, c.NAME, c.COLTYPE, c.LENGTH, c.SCALE, "
            f"c.NULLS, c.REMARKS, c.COLNO FROM {catalog}.SYSCOLUMNS c "
            f"JOIN {catalog}.SYSTABLES t ON t.CREATOR = c.TBCREATOR AND t.NAME = c.TBNAME "
            "WHERE t.TYPE IN ('T','V','M') AND {schema_filter} "
            "ORDER BY c.TBCREATOR, c.TBNAME, c.COLNO"
        ),
        primary_keys=(
            f"SELECT k.TBCREATOR, k.TBNAME, k.NAME FROM {catalog}.SYSCOLUMNS k "
            "WHERE k.KEYSEQ > 0 AND {schema_filter} "
            "ORDER BY k.TBCREATOR, k.TBNAME, k.KEYSEQ"
        ),
        # The child columns are in SYSFOREIGNKEYS (COLSEQ order); the parent
        # columns are the parent's primary key in the same order (KEYSEQ).
        foreign_keys=(
            f"SELECT r.CREATOR, r.TBNAME, f.COLNAME, r.REFTBCREATOR, r.REFTBNAME, p.NAME AS REFCOLNAME "
            f"FROM {catalog}.SYSRELS r "
            f"JOIN {catalog}.SYSFOREIGNKEYS f ON f.CREATOR = r.CREATOR "
            "AND f.TBNAME = r.TBNAME AND f.RELNAME = r.RELNAME "
            f"JOIN {catalog}.SYSCOLUMNS p ON p.TBCREATOR = r.REFTBCREATOR "
            "AND p.TBNAME = r.REFTBNAME AND p.KEYSEQ = f.COLSEQ "
            "WHERE {schema_filter} "
            "ORDER BY r.CREATOR, r.TBNAME, r.RELNAME, f.COLSEQ"
        ),
        schema_column_objects="CREATOR",
        schema_column_columns="c.TBCREATOR",
        schema_column_keys="k.TBCREATOR",
        schema_column_fks="r.CREATOR",
        system_schemas=("NULLID", "SQLJ"),
        system_prefixes=("SYS", "DSN"),
        kinds={"T": "table", "V": "view", "M": "materialized_query_table"},
    )


def ibmi_queries(catalog: str = "QSYS2") -> CatalogQueries:
    # IBM i keeps a short "text" description on every object and column and an
    # optional LONG_COMMENT; prefer the long one, fall back to the text.
    return CatalogQueries(
        objects=(
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, "
            f"COALESCE(LONG_COMMENT, TABLE_TEXT) FROM {catalog}.SYSTABLES "
            "WHERE TABLE_TYPE IN ('T','P','V','L','M') AND {schema_filter} "
            "ORDER BY TABLE_SCHEMA, TABLE_NAME"
        ),
        columns=(
            "SELECT c.TABLE_SCHEMA, c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE, c.LENGTH, "
            "c.NUMERIC_SCALE, c.IS_NULLABLE, COALESCE(c.LONG_COMMENT, c.COLUMN_TEXT), "
            f"c.ORDINAL_POSITION FROM {catalog}.SYSCOLUMNS c "
            f"JOIN {catalog}.SYSTABLES t ON t.TABLE_SCHEMA = c.TABLE_SCHEMA "
            "AND t.TABLE_NAME = c.TABLE_NAME "
            "WHERE t.TABLE_TYPE IN ('T','P','V','L','M') AND {schema_filter} "
            "ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.ORDINAL_POSITION"
        ),
        primary_keys=(
            "SELECT k.TABLE_SCHEMA, k.TABLE_NAME, k.COLUMN_NAME "
            f"FROM {catalog}.SYSCST c JOIN {catalog}.SYSKEYCST k "
            "ON k.CONSTRAINT_SCHEMA = c.CONSTRAINT_SCHEMA AND k.CONSTRAINT_NAME = c.CONSTRAINT_NAME "
            "WHERE c.CONSTRAINT_TYPE = 'PRIMARY KEY' AND {schema_filter} "
            "ORDER BY k.TABLE_SCHEMA, k.TABLE_NAME, k.ORDINAL_POSITION"
        ),
        foreign_keys=(
            "SELECT fk.TABLE_SCHEMA, fk.TABLE_NAME, fk.COLUMN_NAME, "
            "pk.TABLE_SCHEMA AS REF_SCHEMA, pk.TABLE_NAME AS REF_TABLE, pk.COLUMN_NAME AS REF_COLUMN "
            f"FROM {catalog}.SYSREFCST r "
            f"JOIN {catalog}.SYSKEYCST fk ON fk.CONSTRAINT_SCHEMA = r.CONSTRAINT_SCHEMA "
            "AND fk.CONSTRAINT_NAME = r.CONSTRAINT_NAME "
            f"JOIN {catalog}.SYSKEYCST pk ON pk.CONSTRAINT_SCHEMA = r.UNIQUE_CONSTRAINT_SCHEMA "
            "AND pk.CONSTRAINT_NAME = r.UNIQUE_CONSTRAINT_NAME "
            "AND pk.ORDINAL_POSITION = fk.ORDINAL_POSITION "
            "WHERE {schema_filter} "
            "ORDER BY fk.TABLE_SCHEMA, fk.TABLE_NAME, r.CONSTRAINT_NAME, fk.ORDINAL_POSITION"
        ),
        schema_column_objects="TABLE_SCHEMA",
        schema_column_columns="c.TABLE_SCHEMA",
        schema_column_keys="k.TABLE_SCHEMA",
        schema_column_fks="fk.TABLE_SCHEMA",
        # Q* libraries are reserved for IBM-supplied objects (QSYS, QSYS2,
        # QGPL, QTEMP, ...); SYS* holds the SQL catalog and services (SYSIBM,
        # SYSPROC, SYSTOOLS, ...).
        system_schemas=("SQLJ",),
        system_prefixes=("Q", "SYS"),
        kinds={
            "T": "table", "P": "physical_file", "V": "view", "L": "logical_file",
            "M": "materialized_query_table",
        },
    )


def queries_for(platform: str, catalog: str | None = None) -> CatalogQueries:
    """Queries for a platform; `catalog` swaps the catalog schema (tests only)."""
    builder = {LUW: luw_queries, ZOS: zos_queries, IBMI: ibmi_queries}[platform]
    return builder(catalog) if catalog else builder()


def parse_schemas(value: str | None) -> list[str]:
    """Comma-separated schema list -> ordered, de-duplicated list.

    Unquoted Db2 identifiers are stored in uppercase, so names are uppercased
    unless written in double quotes (``"MixedCase"``).
    """
    out: list[str] = []
    seen: set[str] = set()
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        if len(part) >= 2 and part[0] == part[-1] == '"':
            name = part[1:-1]
        else:
            name = part.upper()
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def schema_filter(column: str, schemas: Sequence[str], q: CatalogQueries) -> tuple[str, list]:
    """WHERE fragment + params: the listed schemas, or everything but the system ones."""
    if schemas:
        return f"{column} IN ({', '.join(['?'] * len(schemas))})", list(schemas)
    clauses = []
    params: list = []
    if q.system_schemas:
        clauses.append(f"{column} NOT IN ({', '.join(['?'] * len(q.system_schemas))})")
        params.extend(q.system_schemas)
    for prefix in q.system_prefixes:
        clauses.append(f"{column} NOT LIKE ?")
        params.append(prefix + "%")
    return " AND ".join(clauses) or "1=1", params


def format_dtype(type_name, length=None, scale=None) -> str:
    """Readable column type: VARCHAR(40), DECIMAL(9,2), INTEGER."""
    t = (str(type_name or "")).strip().upper()
    if not t:
        return ""
    if t in ("DECIMAL", "NUMERIC", "DECFLOAT") and length:
        if t == "DECFLOAT":
            return f"DECFLOAT({int(length)})"
        return f"{t}({int(length)},{int(scale or 0)})"
    if any(t.startswith(p) for p in ("CHAR", "VARCHAR", "GRAPHIC", "VARGRAPHIC", "BINARY",
                                     "VARBINARY", "LONG VARCHAR", "CLOB", "BLOB", "DBCLOB")):
        if length and t not in ("LONG VARCHAR",):
            return f"{t}({int(length)})"
    return t


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def build_tables(run: Runner, platform: str, schemas: Sequence[str],
                 q: CatalogQueries | None = None) -> list[Table]:
    """Discover tables/views with columns, comments, PKs and FKs.

    Columns are required; primary and foreign keys are best-effort (a user
    without catalog grants on the key views still gets the tables).
    """
    q = q or queries_for(platform)

    def _run(template: str, column: str) -> list:
        where, params = schema_filter(column, schemas, q)
        return run(template.replace("{schema_filter}", where), params)

    discovery_phase("tables")
    objects = _run(q.objects, q.schema_column_objects)
    tables: dict[tuple[str, str], Table] = {}
    for schema, name, kind, comment in objects:
        schema, name, kind = _s(schema), _s(name), _s(kind)
        tables[(schema, name)] = Table(
            name=f"{schema}.{name}",
            description=_s(comment) or None,
            columns=[],
            pks=[],
            fks=[],
            metadata_json={
                "schema": schema,
                "object_type": q.kinds.get(kind, kind.lower() or "table"),
                "db2_platform": platform,
            },
        )

    discovery_phase("reading_columns")
    rows = _run(q.columns, q.schema_column_columns)
    by_col: dict[tuple[str, str, str], TableColumn] = {}
    for schema, tname, col, dtype, length, scale, _nulls, comment, _pos in discovery_items(
        rows, "columns", label=lambda r: ".".join(_s(v) for v in r[:3])
    ):
        key = (_s(schema), _s(tname))
        table = tables.get(key)
        if table is None:
            continue
        column = TableColumn(
            name=_s(col),
            dtype=format_dtype(dtype, length, scale),
            description=_s(comment) or None,
        )
        table.columns.append(column)
        by_col[(key[0], key[1], column.name)] = column

    discovery_phase("relationships")
    try:
        for schema, tname, col in _run(q.primary_keys, q.schema_column_keys):
            table = tables.get((_s(schema), _s(tname)))
            if table is not None:
                col_obj = by_col.get((_s(schema), _s(tname), _s(col)))
                table.pks.append(col_obj or TableColumn(name=_s(col), dtype=""))
    except IndexingCancelled:
        raise
    except Exception as e:
        logger.info("Db2 (%s) primary keys unavailable: %s", platform, str(e)[:200])

    try:
        for schema, tname, col, rschema, rtable, rcol in _run(q.foreign_keys, q.schema_column_fks):
            table = tables.get((_s(schema), _s(tname)))
            if table is None:
                continue
            table.fks.append(ForeignKey(
                column=by_col.get((_s(schema), _s(tname), _s(col))) or TableColumn(name=_s(col), dtype=""),
                references_name=f"{_s(rschema)}.{_s(rtable)}",
                references_column=by_col.get((_s(rschema), _s(rtable), _s(rcol)))
                or TableColumn(name=_s(rcol), dtype=""),
            ))
    except IndexingCancelled:
        raise
    except Exception as e:
        logger.info("Db2 (%s) foreign keys unavailable: %s", platform, str(e)[:200])

    return list(tables.values())


# Shared by the client descriptions so every Db2 product gets the same hints.
DIALECT_NOTES = """
Db2 SQL notes:
- Row limits: FETCH FIRST n ROWS ONLY (works on every Db2 version and platform);
  OFFSET m ROWS before it for paging. Do not use TOP.
- Identifiers: unquoted names fold to UPPERCASE. Quote mixed-case or special
  names exactly as listed in the schema: "MySchema"."Order Lines".
- Qualify tables as SCHEMA.TABLE.
- Dates: CURRENT DATE / CURRENT TIMESTAMP (no underscore), date arithmetic with
  labeled durations: CURRENT DATE - 30 DAYS, CURRENT DATE - 1 MONTH.
  YEAR(d), MONTH(d), DAYS(d1) - DAYS(d2) for day differences,
  VARCHAR_FORMAT(ts, 'YYYY-MM') to format.
- Strings: single quotes; || or CONCAT for concatenation; TRIM() fixed-width
  CHAR columns before comparing or grouping when padding matters.
- Nulls: COALESCE(x, y). Booleans are often CHAR(1) 'Y'/'N' flags.
- The one-row dummy table is SYSIBM.SYSDUMMY1 (or use VALUES ...).
- Only one SELECT (or WITH ... SELECT) per call; no semicolons.
"""

RELATIVE_DATE_HINT = (
    "Relative dates (Db2): CURRENT DATE, CURRENT DATE - 7 DAYS, CURRENT DATE - 1 MONTH, "
    "FIRST_DAY(CURRENT DATE) or CURRENT DATE - (DAY(CURRENT DATE) - 1) DAYS for month start; "
    "the clock is the DB server's."
)

"""Stored procedures and functions, indexed as part of a SQL source's schema.

A routine is business logic that lives in the database. It is indexed through
the normal `get_schemas()` path, as a `Table` marked
``metadata_json["type"] == "routine"`` (the same way Snowflake semantic views
and Power BI measures ride the schema catalog). Everything built for tables
then applies to routines unchanged: per-agent activation, describe_tables,
YAML include rules, instructions attached to a table, per-user overlays and
pruning on reindex.

What makes a routine different from a table:

* It is not a relation. A procedure cannot be SELECTed from, and the read-only
  SQL guard refuses EXEC / CALL. A function can still be called inside a
  SELECT. The schema context says so, so the agent reads a procedure's source
  and reproduces its logic in a query instead of trying to run it.
* Its parameters are stored as columns (``metadata.role == "parameter"``), so
  the UI's column view and the schema renderer show the signature.
* Its source is stored, capped, in ``metadata_json["routine"]["definition"]``,
  like Power BI stores a measure's DAX. The main schema context shows only the
  signature; describe_tables shows the source.

Overloads (Postgres allows several functions with one name; MySQL allows a
procedure and a function with one name) collapse into ONE entry: the catalog
is keyed by name, and the agent reads all variants together. A routine whose
name equals a table's name (legal in Postgres) is renamed ``name()`` so the
two never merge.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, List, Optional

from app.ai.prompt_formatters import Table, TableColumn

logger = logging.getLogger(__name__)

ROUTINE_TYPE = "routine"
# A long procedure can run to hundreds of KB. The cap keeps the catalog row,
# the schema API payload and a describe_tables result bounded; the head of a
# procedure (signature, comments, main query) is what the agent needs.
MAX_DEFINITION_CHARS = 20_000

PROCEDURE = "procedure"
FUNCTION = "function"


def is_routine(obj: Any) -> bool:
    """True for a routine entry: a Table/row object or its metadata_json dict."""
    meta = obj if isinstance(obj, dict) else getattr(obj, "metadata_json", None)
    return isinstance(meta, dict) and meta.get("type") == ROUTINE_TYPE


def routine_meta(obj: Any) -> dict:
    """The ``routine`` block of a routine entry ({} for anything else)."""
    meta = obj if isinstance(obj, dict) else getattr(obj, "metadata_json", None)
    if not is_routine(meta):
        return {}
    block = meta.get("routine")
    return block if isinstance(block, dict) else {}


def routine_kind(obj: Any) -> Optional[str]:
    """'procedure' | 'function' for a routine entry, else None."""
    if not is_routine(obj):
        return None
    return routine_meta(obj).get("kind") or PROCEDURE


@dataclass
class RoutineVariant:
    """One catalog row: a single procedure or function (one overload)."""

    schema: Optional[str]
    name: str
    kind: str                                   # 'procedure' | 'function'
    params: List[dict] = field(default_factory=list)   # [{name, dtype, mode}]
    arguments: Optional[str] = None             # engine-printed arg list, if any
    returns: Optional[str] = None
    language: Optional[str] = None
    description: Optional[str] = None
    definition: Optional[str] = None
    definition_available: bool = True

    def signature(self, qualified_name: str) -> str:
        if self.arguments is not None:
            inside = self.arguments.strip()
        else:
            parts = []
            for p in self.params:
                seg = " ".join(x for x in (p.get("name"), p.get("dtype")) if x)
                mode = (p.get("mode") or "").upper()
                if mode and mode != "IN":
                    seg = f"{mode} {seg}".strip()
                if seg:
                    parts.append(seg)
            inside = ", ".join(parts)
        sig = f"{self.kind} {qualified_name}({inside})"
        if self.returns:
            sig += f" returns {self.returns}"
        return sig


def build_routine_tables(variants: Iterable[RoutineVariant], table_names: Iterable[str],
                         name_fn: Callable[[Optional[str], str], str]) -> List[Table]:
    """Group catalog rows into one schema entry per routine name.

    `name_fn(schema, name)` must match how the client names its tables
    (``schema.name`` for Postgres / SQL Server, bare for MySQL), so routines
    sort and filter next to the tables they belong with.
    """
    taken = set(table_names)
    grouped: dict = {}
    for v in variants:
        grouped.setdefault(name_fn(v.schema, v.name), []).append(v)

    out: List[Table] = []
    for qualified, group in grouped.items():
        first = group[0]
        name = qualified if qualified not in taken else f"{qualified}()"
        kinds = {v.kind for v in group}
        kind = FUNCTION if kinds == {FUNCTION} else PROCEDURE

        readable = [v for v in group if v.definition_available and v.definition]
        definition = "\n\n".join(v.definition.strip() for v in readable) or None
        truncated = False
        if definition and len(definition) > MAX_DEFINITION_CHARS:
            definition = definition[:MAX_DEFINITION_CHARS]
            truncated = True

        # Parameters as columns: shown in the UI's column view and the schema
        # context. With overloads, the union (first variant's order wins).
        columns: List[TableColumn] = []
        seen = set()
        for v in group:
            for p in v.params:
                pname = p.get("name")
                if not pname or pname in seen:
                    continue
                seen.add(pname)
                meta = {"role": "parameter"}
                if p.get("mode"):
                    meta["mode"] = str(p["mode"]).upper()
                columns.append(TableColumn(name=pname, dtype=p.get("dtype"), metadata=meta))

        block = {
            "kind": kind,
            # The engine's own name, even when the entry was renamed `name()`.
            "signatures": [v.signature(qualified) for v in group],
            "definition_available": any(v.definition_available for v in group),
        }
        for key in ("language", "returns"):
            val = getattr(first, key)
            if val:
                block[key] = val
        if definition:
            block["definition"] = definition
        if truncated:
            block["definition_truncated"] = True
        if len(group) > 1:
            block["overloads"] = len(group)

        meta_json = {"type": ROUTINE_TYPE, "routine": block}
        if first.schema:
            meta_json["schema"] = first.schema
        out.append(Table(
            name=name,
            description=next((v.description for v in group if v.description), None),
            columns=columns,
            pks=[],
            fks=[],
            metadata_json=meta_json,
        ))
    return out


def with_routines(tables: List[Table], discover: Callable[[], List[Table]], *, source: str,
                  client: Any = None, enabled: bool = True) -> List[Table]:
    """Append discovered routines to `tables`.

    Opt-in: `enabled` is the connection's "Index stored procedures &
    functions" setting (``index_routines``). Off means no routine query runs
    at all, and routines indexed earlier are pruned on the next reindex like
    any other entry that is gone.

    Routine discovery is additive: a failure (missing catalog permission, an
    engine version without the catalog view, a timeout) is logged and the
    tables are returned unchanged, so it can never cost a source its table
    index. The failure is also recorded on `client` (see
    `DataSourceClient.unindexed_entry_types`), so the reindex keeps the
    routines indexed on an earlier run, with their agent selection, instead
    of pruning them as dropped.

    No tables means no routines either. Table discovery reports failure by
    returning [] (several clients swallow the error), and refresh_schema
    treats an empty result as "nothing to sync". A result holding only
    routines would instead read as "every table was dropped" and an
    authoritative reindex would prune the whole table catalog.
    """
    from app.data_sources.clients.progress import IndexingCancelled, discovery_phase

    if client is not None:
        client._unindexed_entry_types = set()
    if not enabled or not tables:
        return tables
    discovery_phase("reading_routines")
    try:
        routines = discover() or []
    except IndexingCancelled:
        raise
    except Exception as exc:
        logger.warning("%s: routine discovery failed, indexing tables only: %s", source, exc)
        if client is not None:
            client._unindexed_entry_types = {ROUTINE_TYPE}
        return tables
    return list(tables or []) + routines


# ---------------------------------------------------------------------------
# Catalog queries per engine. Each returns RoutineVariant rows; the client
# passes them to build_routine_tables with its own naming.
# ---------------------------------------------------------------------------

def postgres_variants(conn, schemas: Optional[List[str]]) -> List[RoutineVariant]:
    """Functions and procedures from pg_proc.

    Excluded: system schemas, aggregates / window functions (prokind 'a'/'w',
    which pg_get_functiondef cannot print), and functions an extension owns —
    pgcrypto, postgis and friends install hundreds that are nobody's
    business logic.
    """
    from sqlalchemy import text

    params: dict = {}
    where = [
        "n.nspname NOT IN ('pg_catalog', 'information_schema')",
        "n.nspname NOT LIKE 'pg_toast%'",
        "n.nspname NOT LIKE 'pg_temp%'",
        "p.prokind IN ('f', 'p')",
        "NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass "
        "AND d.objid = p.oid AND d.deptype = 'e')",
    ]
    if schemas:
        keys = []
        for i, s in enumerate(schemas):
            params[f"s{i}"] = s
            keys.append(f":s{i}")
        where.append(f"n.nspname IN ({', '.join(keys)})")
    rows = conn.execute(text(f"""
        SELECT n.nspname, p.proname, p.prokind, l.lanname,
               pg_get_function_arguments(p.oid) AS arguments,
               pg_get_function_result(p.oid) AS returns,
               obj_description(p.oid, 'pg_proc') AS comment,
               pg_get_functiondef(p.oid) AS definition,
               p.proargnames, p.proargmodes,
               ARRAY(SELECT format_type(t, NULL)
                     FROM unnest(coalesce(p.proallargtypes, p.proargtypes::oid[])) AS t) AS argtypes
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        JOIN pg_language l ON l.oid = p.prolang
        WHERE {' AND '.join(where)}
        ORDER BY n.nspname, p.proname, p.oid
    """), params).fetchall()

    mode_names = {"i": "IN", "o": "OUT", "b": "INOUT", "v": "VARIADIC", "t": "TABLE"}
    out: List[RoutineVariant] = []
    for (schema, name, prokind, lang, arguments, returns, comment, definition,
         argnames, argmodes, argtypes) in rows:
        plist = []
        for i, dtype in enumerate(argtypes or []):
            mode = mode_names.get((argmodes or [])[i], "IN") if argmodes else "IN"
            if mode == "TABLE":
                continue  # a RETURNS TABLE column, not a parameter
            pname = (argnames or [])[i] if argnames and i < len(argnames) else None
            plist.append({"name": pname or f"${i + 1}", "dtype": dtype, "mode": mode})
        is_func = prokind == "f"
        out.append(RoutineVariant(
            schema=schema, name=name,
            kind=FUNCTION if is_func else PROCEDURE,
            params=plist,
            arguments=arguments or "",
            returns=(returns or None) if is_func else None,
            language=lang,
            description=comment,
            definition=definition,
            # pg_get_functiondef needs no privilege beyond seeing the catalog.
            definition_available=definition is not None,
        ))
    return out


def _mssql_type(type_name: Optional[str], max_length, precision, scale) -> Optional[str]:
    if not type_name:
        return None
    t = type_name.lower()
    if t in ("varchar", "char", "varbinary", "binary"):
        return f"{t}({'max' if max_length == -1 else max_length})"
    if t in ("nvarchar", "nchar"):
        return f"{t}({'max' if max_length == -1 else (max_length or 0) // 2})"
    if t in ("decimal", "numeric"):
        return f"{t}({precision},{scale})"
    return t


def mssql_variants(conn, schemas: Optional[List[str]]) -> List[RoutineVariant]:
    """Procedures and functions from sys.objects / sys.sql_modules.

    Joined by object_id, never by OBJECT_ID('schema.name'), so names with
    spaces, dots or brackets resolve. `definition` is NULL when the account
    can see the object but lacks VIEW DEFINITION; that is recorded as
    definition_available=False rather than as an empty routine.
    """
    from sqlalchemy import text

    params: dict = {}
    where = ["o.type IN ('P', 'PC', 'FN', 'IF', 'TF', 'FS', 'FT')", "o.is_ms_shipped = 0"]
    if schemas:
        keys = []
        for i, s in enumerate(schemas):
            params[f"s{i}"] = s
            keys.append(f":s{i}")
        where.append(f"s.name IN ({', '.join(keys)})")
    where_sql = " AND ".join(where)
    routines = conn.execute(text(f"""
        SELECT o.object_id, s.name, o.name, o.type,
               m.definition,
               CAST(ep.value AS NVARCHAR(MAX)) AS comment
        FROM sys.objects o
        JOIN sys.schemas s ON s.schema_id = o.schema_id
        LEFT JOIN sys.sql_modules m ON m.object_id = o.object_id
        LEFT JOIN sys.extended_properties ep
               ON ep.class = 1 AND ep.major_id = o.object_id
              AND ep.minor_id = 0 AND ep.name = 'MS_Description'
        WHERE {where_sql}
        ORDER BY s.name, o.name
    """), params).fetchall()
    prm = conn.execute(text(f"""
        SELECT p.object_id, p.parameter_id, p.name, TYPE_NAME(p.user_type_id),
               p.max_length, p.precision, p.scale, p.is_output
        FROM sys.parameters p
        JOIN sys.objects o ON o.object_id = p.object_id
        JOIN sys.schemas s ON s.schema_id = o.schema_id
        WHERE {where_sql}
        ORDER BY p.object_id, p.parameter_id
    """), params).fetchall()

    by_obj: dict = {}
    returns: dict = {}
    for obj_id, pid, pname, tname, max_len, prec, scale, is_out in prm:
        dtype = _mssql_type(tname, max_len, prec, scale)
        if pid == 0:  # a scalar function's return value
            returns[obj_id] = dtype
            continue
        by_obj.setdefault(obj_id, []).append(
            {"name": pname, "dtype": dtype, "mode": "OUT" if is_out else "IN"})

    out: List[RoutineVariant] = []
    for obj_id, schema, name, otype, definition, comment in routines:
        otype = (otype or "").strip()
        is_func = otype in ("FN", "IF", "TF", "FS", "FT")
        ret = returns.get(obj_id)
        if is_func and not ret and otype in ("IF", "TF", "FT"):
            ret = "table"
        clr = otype in ("PC", "FS", "FT")
        out.append(RoutineVariant(
            schema=schema, name=name,
            kind=FUNCTION if is_func else PROCEDURE,
            params=by_obj.get(obj_id, []),
            returns=ret if is_func else None,
            language="CLR" if clr else "T-SQL",
            description=comment,
            definition=definition,
            # CLR routines have no T-SQL source to show, whatever the grants.
            definition_available=definition is not None,
        ))
    return out


def mysql_variants(conn, database: str) -> List[RoutineVariant]:
    """Procedures and functions from information_schema (MySQL and MariaDB).

    ROUTINE_DEFINITION is the body only, and NULL for an account that is not
    the routine's definer and lacks SHOW_ROUTINE; the latter is recorded as
    definition_available=False. Parameters are keyed by (type, name) because
    a procedure and a function may share a name.
    """
    from sqlalchemy import text

    routines = conn.execute(text("""
        SELECT ROUTINE_NAME, ROUTINE_TYPE, DTD_IDENTIFIER, ROUTINE_DEFINITION, ROUTINE_COMMENT
        FROM information_schema.ROUTINES
        WHERE ROUTINE_SCHEMA = :db
        ORDER BY ROUTINE_NAME, ROUTINE_TYPE
    """), {"db": database}).fetchall()
    prm = conn.execute(text("""
        SELECT SPECIFIC_NAME, ROUTINE_TYPE, PARAMETER_NAME, DTD_IDENTIFIER, PARAMETER_MODE, ORDINAL_POSITION
        FROM information_schema.PARAMETERS
        WHERE SPECIFIC_SCHEMA = :db
        ORDER BY SPECIFIC_NAME, ROUTINE_TYPE, ORDINAL_POSITION
    """), {"db": database}).fetchall()

    by_key: dict = {}
    for spec, rtype, pname, dtype, mode, ordinal in prm:
        if not pname or int(ordinal or 0) == 0:
            continue  # ordinal 0 is a function's return value
        by_key.setdefault((str(rtype).upper(), spec), []).append(
            {"name": pname, "dtype": dtype, "mode": mode or "IN"})

    out: List[RoutineVariant] = []
    for name, rtype, dtd, body, comment in routines:
        rtype = str(rtype).upper()
        is_func = rtype == "FUNCTION"
        plist = by_key.get((rtype, name), [])
        variant = RoutineVariant(
            schema=None, name=name,
            kind=FUNCTION if is_func else PROCEDURE,
            params=plist,
            returns=(dtd or None) if is_func else None,
            language="SQL",
            description=comment or None,
            definition_available=body is not None,
        )
        if body is not None:
            # Only the body is stored; prepend the header so the source reads
            # as the routine it is.
            rest = variant.signature(name).split(" ", 1)[1].replace(" returns ", " RETURNS ")
            variant.definition = f"CREATE {variant.kind.upper()} {rest}\n{body}"
        out.append(variant)
    return out

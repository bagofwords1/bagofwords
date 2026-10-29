"""Shared helpers for discovering SQL routines (stored procedures / functions).

A client opts into routine indexing by delegating its get_routines /
get_routine_definition to one of these, passing its own `execute_query` (which
returns a pandas DataFrame). Everything here runs read-only catalog SELECTs and
returns `app.ai.prompt_formatters.Routine` objects carrying signatures only —
bodies are fetched on demand, never during discovery.

Only the MySQL family shares a catalog shape clean enough to generalize
(`information_schema.ROUTINES`, filtered by database). Engines whose catalogs
differ materially (Oracle, Snowflake, BigQuery, Sybase) implement their own
methods in their client.
"""
from typing import List, Optional

from app.ai.prompt_formatters import Routine


def sql_str(value) -> str:
    """Single-quote-escape a literal for inline embedding in a catalog query."""
    return "'" + str(value).replace("'", "''") + "'"


def mysql_family_routines(execute_query, database: str) -> List[Routine]:
    """Routines for MySQL / MariaDB, where the schema IS the database.

    `information_schema.ROUTINES` gives name, kind and return type;
    `information_schema.PARAMETERS` the parameters (ordinal 0 is a function's
    return value and is skipped — the return type comes from the routine row).
    """
    routines_sql = f"""
        SELECT ROUTINE_SCHEMA, ROUTINE_NAME, ROUTINE_TYPE, DTD_IDENTIFIER
        FROM information_schema.ROUTINES
        WHERE ROUTINE_SCHEMA = {sql_str(database)}
        ORDER BY ROUTINE_NAME
    """
    params_sql = f"""
        SELECT SPECIFIC_NAME, PARAMETER_NAME, DTD_IDENTIFIER, PARAMETER_MODE, ORDINAL_POSITION
        FROM information_schema.PARAMETERS
        WHERE SPECIFIC_SCHEMA = {sql_str(database)}
        ORDER BY SPECIFIC_NAME, ORDINAL_POSITION
    """
    from app.ai.prompt_formatters import RoutineParam

    params_df = execute_query(params_sql)
    by_name: dict = {}
    if params_df is not None and not params_df.empty:
        for _, p in params_df.iterrows():
            if not p.get("PARAMETER_NAME") or int(p.get("ORDINAL_POSITION") or 0) == 0:
                continue
            by_name.setdefault(p.get("SPECIFIC_NAME"), []).append(
                RoutineParam(
                    name=p.get("PARAMETER_NAME"),
                    dtype=p.get("DTD_IDENTIFIER"),
                    mode=p.get("PARAMETER_MODE"),
                )
            )

    routines: List[Routine] = []
    df = execute_query(routines_sql)
    if df is None or df.empty:
        return routines
    for _, r in df.iterrows():
        schema_name = r.get("ROUTINE_SCHEMA")
        name = r.get("ROUTINE_NAME")
        is_func = str(r.get("ROUTINE_TYPE")).upper() == "FUNCTION"
        routines.append(Routine(
            name=f"{schema_name}.{name}",
            schema_name=schema_name,
            routine_type="function" if is_func else "procedure",
            params=by_name.get(name) or None,
            returns=(r.get("DTD_IDENTIFIER") or None) if is_func else None,
        ))
    return routines


def mysql_family_definition(execute_query, database: str, qualified_name: str) -> Optional[str]:
    """Body of one MySQL / MariaDB routine via ROUTINE_DEFINITION.

    `qualified_name` is "schema.name"; the schema part is ignored in favor of
    the connection's database (MySQL routines live in one database)."""
    name = str(qualified_name).split(".", 1)[-1]
    sql = f"""
        SELECT ROUTINE_DEFINITION
        FROM information_schema.ROUTINES
        WHERE ROUTINE_SCHEMA = {sql_str(database)}
          AND ROUTINE_NAME = {sql_str(name)}
        LIMIT 1
    """
    df = execute_query(sql)
    if df is None or df.empty:
        return None
    return df.iloc[0]["ROUTINE_DEFINITION"]

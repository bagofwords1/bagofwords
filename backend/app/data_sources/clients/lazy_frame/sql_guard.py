"""Read-only gate for SQL that reaches a DuckDB connection on the lazy path."""

from __future__ import annotations


def ensure_single_read_statement(sql: str, *, allowed_keywords, error_message: str) -> None:
    """Require exactly one read-only statement whose leading keyword is allowed.

    Directory confinement limits WHERE a DuckDB query can write, but COPY can
    still fill the confined directory outside every spill budget — so every
    surface that accepts SQL (LazyFrame.sql, the native-DuckDB streamer)
    applies this gate as defense in depth. The leading-keyword check catches
    what a lexical scan can (comments stripped first); duckdb's own statement
    parse enforces single-statement, SELECT-typed input.
    """
    import re

    import duckdb

    stripped = re.sub(r"\A(?:\s|--[^\n]*(?:\n|$)|/\*.*?\*/)*", "", sql, flags=re.S)
    first = re.match(r"[A-Za-z]+", stripped)
    statements = duckdb.extract_statements(sql)
    if (
        first is None
        or first.group(0).upper() not in allowed_keywords
        or len(statements) != 1
        or statements[0].type != duckdb.StatementType.SELECT
    ):
        raise ValueError(error_message)

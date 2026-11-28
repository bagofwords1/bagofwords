"""FastQueryClient — serves materialized custom tables to the agent.

Built per request from the custom tables a given agent has **activated**. Each
call to `connect()` creates a throwaway in-memory DuckDB, attaches the relevant
encrypted artifacts read-only, registers one view per relation, and then locks
the session down before any agent-generated SQL runs.

The lockdown matters. With pandas denied filesystem access by the encrypted
artifacts, DuckDB SQL becomes the remaining surface: without it, generated SQL
could ATTACH another connection's artifact, `read_parquet('/etc/...')`, or
exfiltrate via `COPY (SELECT ...) TO '/tmp/x.csv'`.

Registering **only activated relations** is the authorization boundary, and it
is structural rather than advisory — an agent cannot name a relation that was
never put in its catalog.
"""

import logging
import re
import time
from contextlib import contextmanager
from typing import Generator, List, Optional

import duckdb
import pandas as pd

from app.ai.code_execution.duckdb_guard import inspect_duckdb_sql
from app.ai.code_execution.sql_guard import DENIED_FUNCTIONS, duckdb_read
from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
from app.data_sources.clients.base import Capability, DataSourceClient
from app.data_sources.fast.rls import Filter

logger = logging.getLogger(__name__)

# DuckDB introspection that reports server-side state — attached artifact file
# paths (duckdb_databases / PRAGMA database_list), directory settings, the
# extension install path — or that evaluates a table name from a string
# (query / query_table, a way around this list). None of it is needed to
# query the registered relations, so it is refused before execution.
_BLOCKED_INTROSPECTION = re.compile(
    # Catalog functions, callable with or without parentheses, and the
    # settings view.
    r"\b(?:duckdb_databases|duckdb_settings|duckdb_extensions|duckdb_secrets|"
    r"duckdb_temporary_files|duckdb_memory|duckdb_logs?|(?:pragma_)?database_list|pg_settings)\b"
    # Scalar/table functions, only when called (a column named `query` is fine).
    r"|\b(?:current_setting|which_secret|getenv|query_table|query)\s*\("
    # The catalog an artifact is attached under. Relations are queried by
    # their own names; reaching through the attachment would bypass the view
    # that hides the loader's bookkeeping columns.
    r"|\bbow_a\d+\b",
    re.IGNORECASE,
)


def assert_no_blocked_introspection(sql: str) -> None:
    if sql and _BLOCKED_INTROSPECTION.search(sql):
        raise ValueError(
            "System introspection functions are not available on accelerated data; "
            "query the registered tables instead."
        )


# A dynamic PIVOT (no IN list) is two statements to DuckDB.
_PIVOT = re.compile(r"\bPIVOT\b", re.IGNORECASE)

# The calls refused on DuckDB's own reading of the statement, so a quoted
# name, or a comment or space between a name and its `(`, changes nothing:
# the DuckDB connections' denied functions (files, settings, secrets, the
# environment, SQL held in a string) and the rest of DuckDB's file readers,
# logging functions and server-state catalog functions.
_BLOCKED_FUNCTION = re.compile(
    DENIED_FUNCTIONS["duckdb"].pattern
    + r"|which_secret|sniff_csv|parquet_.*|json_execute_serialized_sql"
    r"|enable_logging|disable_logging|write_log|truncate_duckdb_logs|duckdb_logs?|duckdb_log_contexts"
    r"|duckdb_databases|duckdb_temporary_files|duckdb_memory|(?:pragma_)?database_list",
    re.DOTALL,
)
# The catalog views of that kind, and the catalogs artifacts are attached
# under, refused as any part of a table name.
_BLOCKED_TABLE = re.compile(
    r"duckdb_(?:databases|settings|extensions|secrets|temporary_files|memory|logs?|log_contexts)"
    r"|(?:pragma_)?database_list|pg_settings|bow_a\d+"
)


def assert_no_blocked_calls(sql: str) -> None:
    """Refuse `sql`, one statement its caller has taken as a SELECT on a
    locked engine, when DuckDB's parser finds a blocked call
    (`_BLOCKED_FUNCTION`) or catalog name (`_BLOCKED_TABLE`) in it. The
    DuckDB connections' reads (DESCRIBE, SHOW, PRAGMA table_info, ...) are
    read as those connections read them; text neither reading takes is
    refused."""
    read = duckdb_read(sql)
    names = set(read.name or ()) if read is not None else set()
    select = sql if read is None else read.select
    calls: set[str] = set()
    if select is not None:
        stmt = inspect_duckdb_sql(select)
        calls = set(stmt.functions | stmt.table_functions)
        names |= {part for table in stmt.tables for part in table}
    blocked = sorted(c for c in calls if _BLOCKED_FUNCTION.fullmatch(c))
    blocked += sorted(n for n in names if _BLOCKED_TABLE.fullmatch(n))
    if blocked:
        raise ValueError(
            f"{blocked[0]} is not available on this connection: system introspection, file and logging "
            "functions are disabled. Query its tables instead."
        )


class FastRelation:
    """One activated custom table, resolved to a readable artifact."""

    def __init__(
        self,
        name: str,
        artifact_path: str,
        artifact_key: str,
        columns: list,
        as_of: Optional[str] = None,
        row_count: int = 0,
        description: Optional[str] = None,
        rls_filter: Optional["Filter"] = None,
    ):
        self.name = name
        self.artifact_path = artifact_path
        self.artifact_key = artifact_key
        self.columns = columns or []
        self.as_of = as_of
        self.row_count = row_count
        self.description = description
        # Compiled once per request against the asking identity. None means the
        # relation carries no policy; a Filter may still be unrestricted.
        self.rls_filter = rls_filter

    @property
    def is_row_filtered(self) -> bool:
        f = self.rls_filter
        return f is not None and f.is_row_filtered

    @property
    def is_column_filtered(self) -> bool:
        f = self.rls_filter
        return f is not None and f.is_column_filtered

    @property
    def needs_copy(self) -> bool:
        """Goes through the copy+detach path (not served as a plain view).

        True for any governed relation — row-filtered, column-projected, or
        denied. The attach-failure guard keys off this: if registration stops
        between ATTACH and DETACH for any of these, the raw artifact is still
        nameable and the session must refuse to serve."""
        f = self.rls_filter
        return f is not None and not f.unrestricted

    def visible_columns(self) -> list:
        """The column descriptors this identity may see — what the schema
        context and the projection must agree on. Narrowed by the compiled
        filter's `allowed_columns`; the full list when nothing is restricted."""
        f = self.rls_filter
        allowed = getattr(f, "allowed_columns", None) if f is not None else None
        if allowed is None:
            return self.columns
        allow = set(allowed)
        return [c for c in self.columns if c.get("name") in allow]


def lock_query_session(con):
    """Seal a session after trusted code has registered its authorized relations.

    DuckDB allows a session its own temp directory under the lock, and the
    default one (`.tmp` under the working directory) is shared by every
    in-memory session of the process; with none set, spilling is off and no
    temp directory is reachable."""
    con.execute("SET temp_directory=''")
    con.execute("SET python_enable_replacements=false")
    con.execute("SET enable_external_access=false")
    con.execute("SET lock_configuration=true")


class FastQueryClient(DataSourceClient):
    """DuckDB over encrypted local artifacts. Read-only by construction."""

    # DuckDB SQL underneath: '' is the only quote escape (sqlglot duckdb
    # tokenizer).
    sql_literal_style = "standard"

    # Rendered into codegen prompts (<connection_clients>) so generated queries
    # express time windows with the engine's own relative date functions instead
    # of literal dates that go stale when saved code is re-executed.
    relative_date_hint = "Relative dates (DuckDB SQL): current_date, current_date - INTERVAL 7 DAY, date_trunc('month', current_date); the clock is the app server's local time."

    capabilities = {Capability.QUERY}

    def __init__(self, relations: List[FastRelation], connection_name: str = ""):
        super().__init__()
        self.relations = relations or []
        self.connection_name = connection_name

    # -- serving ----------------------------------------------------------

    @contextmanager
    def connect(self) -> Generator[duckdb.DuckDBPyConnection, None, None]:
        con = None
        try:
            # No temp directory from the start: the row-filtered copies made
            # before the lock never spill where another session can read.
            con = duckdb.connect(database=":memory:", config={"temp_directory": ""})
            attached = 0
            for i, rel in enumerate(self.relations):
                if not rel.artifact_path:
                    logger.warning(
                        "fast.connect.no_artifact", extra={"relation": rel.name}
                    )
                    continue
                alias = f"bow_a{i}"
                safe_name = rel.name.replace('"', '""')
                try:
                    # Extracts are write-once (a refresh writes a new file).
                    from app.services import storage
                    storage.ensure_local(rel.artifact_path, fresh=False)
                    con.execute(
                        f"ATTACH '{rel.artifact_path}' AS {alias} "
                        f"(ENCRYPTION_KEY '{rel.artifact_key}', READ_ONLY)"
                    )
                    self._register_relation(con, rel, alias, safe_name, i)
                    attached += 1
                except Exception as e:
                    # A single broken artifact must not take down the whole
                    # session — the other relations still serve.
                    logger.error(
                        "fast.connect.attach_failed",
                        extra={"relation": rel.name, "error": str(e)},
                    )
                    if rel.needs_copy:
                        # Except when the failure was on a governed relation
                        # (row-filtered, column-projected, or denied): its
                        # registration copies the slice and DETACHes the source,
                        # so a failure between the ATTACH and the DETACH leaves
                        # the unfiltered artifact nameable (bow_aN). Serving the
                        # rest of the session would expose every row and column.
                        raise RuntimeError(
                            f"Refusing to serve accelerated data: secured "
                            f"relation '{rel.name}' could not be registered "
                            f"safely ({e})"
                        )

            # Lock the session down AFTER attaching, BEFORE any agent SQL runs.
            # enable_external_access=false blocks ATTACH of other files,
            # read_parquet/read_csv of arbitrary paths, and COPY ... TO.
            # lock_configuration=true stops generated SQL re-enabling either.
            lock_query_session(con)

            logger.info(
                "fast.connect.ready",
                extra={"relations": attached, "connection": self.connection_name},
            )
            yield con
        finally:
            if con is not None:
                con.close()

    def _register_relation(self, con, rel: "FastRelation", alias: str,
                           safe_name: str, index: int) -> None:
        """Put `rel` in the session catalog under its own name — filtered.

        The raw relation is never registered under a name the agent can reach.
        Whatever ends up here IS what the agent can see, so a policy cannot be
        bypassed by a cleverly-shaped query; there is no unfiltered object to
        find.

        Allowed values are bound through a session-local table rather than
        interpolated. DuckDB views take no parameters, and a user's own
        `department` string must never become SQL text.
        """
        source = f'{alias}."{safe_name}"'
        f = rel.rls_filter
        # Run-lineage columns are bookkeeping, not data: an agent's `SELECT *`
        # must return the admin's columns and nothing else.
        star = self._visible_columns(con, alias, rel.name)

        if f is None or f.unrestricted:
            con.execute(f'CREATE VIEW "{safe_name}" AS SELECT {star} FROM {source}')
            return

        # Column security replaces the `*`/`* EXCLUDE` projection with the
        # identity's explicit allowed list. That list is drawn from the admin's
        # data columns, so it already excludes the run-lineage bookkeeping
        # columns `star` would have dropped — there is nothing to EXCLUDE on top
        # of a positive projection. `star` stands when only rows are restricted.
        projection = self._projection(rel, f, star)

        if f.deny_all or f.column_denied:
            # No rows (row deny) or no visible columns (every column restricted,
            # none granted) — serve an empty relation that still keeps its shape,
            # so the schema context can advertise it and an agent gets an empty
            # answer rather than "no such table", which reads as a bug.
            logger.info(
                "fast.rls.denied",
                extra={"relation": rel.name, "reason": f.reason},
            )
            con.execute(
                f'CREATE TABLE "{safe_name}" AS SELECT {projection} FROM {source} WHERE 1 = 0'
            )
            self._detach(con, alias, rel)
            return

        # Row predicate is optional: a column-only policy has no values table
        # and no WHERE, just the narrowed projection copied into the session.
        #
        # Cost note (column-only): with rows unrestricted this copies ALL rows
        # (narrowed to the allowed columns) into the session, because a plain
        # view over the attached artifact would be bypassable via `bow_aN`, so
        # the source must be DETACHed — and that needs a materialized copy. A
        # row filter keeps the copy proportional to the user's slice; a
        # column-only policy does not. On a very large relation this is a per
        # query cost; a per-identity cached projection is the follow-up if it
        # bites. (RLS never hits this: unrestricted serves a view at zero copy.)
        where = ""
        values_table = None
        if f.allowed_values is not None:
            values_table = f"bow_rls_{index}"
            safe_column = str(f.column).replace('"', '""')
            con.execute(f'CREATE TEMP TABLE "{values_table}" (v VARCHAR)')
            con.executemany(
                f'INSERT INTO "{values_table}" VALUES (?)',
                [(str(v),) for v in f.allowed_values],
            )
            where = (
                f' WHERE CAST("{safe_column}" AS VARCHAR) IN '
                f'(SELECT v FROM "{values_table}")'
            )
        con.execute(
            f'CREATE TABLE "{safe_name}" AS SELECT {projection} FROM {source}{where}'
        )
        self._detach(con, alias, rel)
        if values_table is not None:
            con.execute(f'DROP TABLE "{values_table}"')
        logger.info(
            "fast.rls.applied",
            extra={
                "columns_visible": None if f.allowed_columns is None else len(f.allowed_columns),
                "relation": rel.name,
                "column": f.column,
                "values": len(f.allowed_values or []),
                "reason": f.reason,
            },
        )

    @staticmethod
    def _projection(rel: "FastRelation", f: "Filter", star: str) -> str:
        """The SELECT list for a restricted relation.

        `star` (the `*`/`* EXCLUDE (...)` that drops run-lineage columns) when
        no column is restricted. Otherwise an explicit, quoted list in the
        relation's own column order — intersected with the materialized columns
        so a stale policy name cannot inject, and never interpolated from the
        policy text. An empty intersection yields a single placeholder column,
        never `star`: a stale or empty allow-list must hide more, not less.
        """
        if f is None or f.allowed_columns is None:
            return star
        allow = set(f.allowed_columns)
        names = [c.get("name") for c in (rel.columns or []) if c.get("name") in allow]
        if not names:
            # Fail closed: never widen to `star`. A zero-column table is not
            # possible, so serve a single placeholder (callers pair this with
            # deny_all, so no rows are returned either).
            return 'CAST(NULL AS INTEGER) AS "no_visible_columns"'
        return ", ".join('"' + str(n).replace('"', '""') + '"' for n in names)

    @staticmethod
    def _visible_columns(con, alias: str, relation: str) -> str:
        """`*` minus the loader's hidden columns, where the artifact has them
        (one written before run lineage existed does not)."""
        from app.data_sources.fast.loader import HIDDEN_COLUMNS

        present = {
            r[0] for r in con.execute(
                "SELECT column_name FROM duckdb_columns() "
                "WHERE database_name = ? AND table_name = ?",
                [alias, relation],
            ).fetchall()
        }
        hidden = [c for c in HIDDEN_COLUMNS if c in present]
        if not hidden:
            return "*"
        return "* EXCLUDE (" + ", ".join(f'"{c}"' for c in hidden) + ")"

    @staticmethod
    def _detach(con, alias: str, rel: "FastRelation") -> None:
        """Remove the raw artifact from the session catalog.

        A filtered VIEW is not enough on its own. ATTACH puts the artifact in
        the catalog under its alias, and `SELECT * FROM bow_a0.sales` reads
        straight past the view — the alias is discoverable via
        `duckdb_databases()`, so this is not obscurity that could be left
        alone. Verified: before this DETACH, that query returned all four rows
        of a relation filtered to two.

        So a filtered relation is COPIED into the session database and the
        source detached, leaving no unfiltered object to name. The copy costs
        memory proportional to the user's slice rather than the whole relation,
        which is the point of a slice; the unrestricted case still serves
        straight off the attached file and pays nothing.

        Failing to detach is not survivable — it would mean serving a relation
        whose filter can be walked around — so it raises rather than logging.
        """
        try:
            con.execute(f"DETACH {alias}")
        except Exception as e:
            raise RuntimeError(
                f"Refusing to serve '{rel.name}': the unfiltered artifact could "
                f"not be removed from the session catalog ({e})"
            )

    def execute_query(self, sql: str) -> pd.DataFrame:
        assert_no_blocked_introspection(sql)
        t0 = time.perf_counter()
        with self.connect() as con:
            # Read on the locked engine, before anything runs.
            statements = con.extract_statements(sql)
            if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                hint = (" A PIVOT needs an IN list (PIVOT ... ON <column> IN (...)), or use SELECT ... PIVOT (...)."
                        if _PIVOT.search(sql) else "")
                raise ValueError("Only one read-only SELECT is supported on accelerated data." + hint)
            assert_no_blocked_calls(sql)
            df = con.execute(sql).df()
        logger.info(
            "fast.query.done",
            extra={
                "rows": len(df),
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            },
        )
        return df

    # -- catalog ----------------------------------------------------------

    def get_tables(self) -> List[Table]:
        # Columns the asking identity may not see are left out of the schema the
        # agent is given, not only out of the rows it reads: otherwise the model
        # writes `SELECT salary` against a projection that no longer has it and
        # the query errors. Enforcement is still the projection in
        # `_register_relation`; this keeps the advertised shape honest.
        return [
            Table(
                name=rel.name,
                description=rel.description,
                columns=[
                    TableColumn(name=c.get("name"), dtype=c.get("dtype"))
                    for c in rel.visible_columns()
                ],
                pks=[],
                fks=[],
            )
            for rel in self.relations
        ]

    def get_schemas(self) -> List[Table]:
        return self.get_tables()

    def get_schema(self, table_name: str) -> Optional[Table]:
        for t in self.get_tables():
            if t.name == table_name:
                return t
        return None

    def prompt_schema(self) -> str:
        return TableFormatter(self.get_tables()).table_str

    def test_connection(self) -> dict:
        try:
            with self.connect() as con:
                con.execute("SELECT 1").fetchall()
            return {"success": True, "message": "Accelerated data is readable"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    @property
    def description(self) -> str:
        """What the coder sees in <connection_clients>.

        Three things the model must know: the dialect is DuckDB (source dialect
        rules do not apply), scans are local and therefore cheap, and how fresh
        each relation is.
        """
        lines = [
            "**Accelerated (FAST) data — local, pre-materialized.**",
            "",
            "This client speaks **DuckDB SQL**. It is NOT the source database:",
            "the source's dialect rules (Oracle ROWNUM, SOQL restrictions, T-SQL",
            "TOP, ...) do not apply here. Write standard SQL — real JOINs, CTEs,",
            "window functions and subqueries all work.",
            "",
            "**Scans are cheap.** The data is a local file, not a remote database.",
            "Do not pre-aggregate defensively or avoid full scans to save cost —",
            "query it directly and let DuckDB do the work.",
            "",
            "Available relations:",
        ]
        for rel in self.relations:
            # Only the columns this identity may see — the agent must not be told
            # a restricted column exists, or it will write SQL that references it.
            cols = ", ".join(c.get("name", "") for c in (rel.visible_columns() or [])[:40])
            freshness = f" (data as of {rel.as_of})" if rel.as_of else ""
            desc = f" — {rel.description}" if rel.description else ""
            rls = (
                " **Row-filtered for the current user** — totals here are that "
                "user's slice, not the whole relation."
                if rel.is_row_filtered else ""
            )
            cls = (
                " **Column-restricted for the current user** — some columns of "
                "this relation are not available to you; use only those listed."
                if rel.is_column_filtered else ""
            )
            lines.append(
                f"- `{rel.name}`{desc}: {rel.row_count:,} rows{freshness}. "
                f"Columns: {cols}{rls}{cls}"
            )
        lines += [
            "",
            "These relations are refreshed on a schedule, so figures reflect the",
            "'as of' time shown above rather than this instant. Say so when the",
            "recency of the answer matters.",
        ]
        return "\n".join(lines)

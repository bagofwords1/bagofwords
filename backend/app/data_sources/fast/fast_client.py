"""FastQueryClient — serves materialized custom queries to the agent.

Built per request from the custom queries a given agent has **activated**. Each
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
import time
from contextlib import contextmanager
from typing import Generator, List, Optional

import duckdb
import pandas as pd

from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
from app.data_sources.clients.base import Capability, DataSourceClient
from app.data_sources.fast.rls import Filter

logger = logging.getLogger(__name__)


class FastRelation:
    """One activated custom query, resolved to a readable artifact."""

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
        return f is not None and not f.unrestricted


class FastQueryClient(DataSourceClient):
    """DuckDB over encrypted local artifacts. Read-only by construction."""

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
            con = duckdb.connect(database=":memory:")
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
                    if rel.is_row_filtered:
                        # Except when the failure was on a row-filtered
                        # relation: registration may have stopped after the
                        # ATTACH and before the DETACH, leaving the unfiltered
                        # artifact nameable. Serving the rest of the session
                        # would mean serving that.
                        raise RuntimeError(
                            f"Refusing to serve accelerated data: row-filtered "
                            f"relation '{rel.name}' could not be registered "
                            f"safely ({e})"
                        )

            # Lock the session down AFTER attaching, BEFORE any agent SQL runs.
            # enable_external_access=false blocks ATTACH of other files,
            # read_parquet/read_csv of arbitrary paths, and COPY ... TO.
            # lock_configuration=true stops generated SQL re-enabling either.
            for pragma in (
                "SET enable_external_access = false",
                "SET lock_configuration = true",
            ):
                try:
                    con.execute(pragma)
                except Exception as e:
                    logger.error(
                        "fast.connect.lockdown_failed",
                        extra={"pragma": pragma, "error": str(e)},
                    )
                    raise RuntimeError(
                        f"Refusing to serve accelerated data without sandbox "
                        f"lockdown ({pragma}): {e}"
                    )

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

        if f is None or f.unrestricted:
            con.execute(f'CREATE VIEW "{safe_name}" AS SELECT * FROM {source}')
            return

        if f.deny_all:
            # The relation still exists and still has its shape — the schema
            # context advertises it, and an agent asking for it should get an
            # empty answer rather than "no such table", which reads as a bug.
            logger.info(
                "fast.rls.denied",
                extra={"relation": rel.name, "reason": f.reason},
            )
            con.execute(
                f'CREATE TABLE "{safe_name}" AS SELECT * FROM {source} WHERE 1 = 0'
            )
            self._detach(con, alias, rel)
            return

        values_table = f"bow_rls_{index}"
        safe_column = str(f.column).replace('"', '""')
        con.execute(f'CREATE TEMP TABLE "{values_table}" (v VARCHAR)')
        con.executemany(
            f'INSERT INTO "{values_table}" VALUES (?)',
            [(str(v),) for v in f.allowed_values],
        )
        con.execute(
            f'CREATE TABLE "{safe_name}" AS SELECT * FROM {source} '
            f'WHERE CAST("{safe_column}" AS VARCHAR) IN '
            f'(SELECT v FROM "{values_table}")'
        )
        self._detach(con, alias, rel)
        con.execute(f'DROP TABLE "{values_table}"')
        logger.info(
            "fast.rls.applied",
            extra={
                "relation": rel.name,
                "column": f.column,
                "values": len(f.allowed_values or []),
                "reason": f.reason,
            },
        )

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
        t0 = time.perf_counter()
        with self.connect() as con:
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
        return [
            Table(
                name=rel.name,
                description=rel.description,
                columns=[
                    TableColumn(name=c.get("name"), dtype=c.get("dtype"))
                    for c in rel.columns
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
            cols = ", ".join(c.get("name", "") for c in (rel.columns or [])[:40])
            freshness = f" (data as of {rel.as_of})" if rel.as_of else ""
            desc = f" — {rel.description}" if rel.description else ""
            rls = (
                " **Row-filtered for the current user** — totals here are that "
                "user's slice, not the whole relation."
                if rel.is_row_filtered else ""
            )
            lines.append(
                f"- `{rel.name}`{desc}: {rel.row_count:,} rows{freshness}. "
                f"Columns: {cols}{rls}"
            )
        lines += [
            "",
            "These relations are refreshed on a schedule, so figures reflect the",
            "'as of' time shown above rather than this instant. Say so when the",
            "recency of the answer matters.",
        ]
        return "\n".join(lines)

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
from app.data_sources.fast import artifacts

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
    ):
        self.name = name
        self.artifact_path = artifact_path
        self.artifact_key = artifact_key
        self.columns = columns or []
        self.as_of = as_of
        self.row_count = row_count
        self.description = description


class FastQueryClient(DataSourceClient):
    """DuckDB over encrypted local artifacts. Read-only by construction."""

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
                    con.execute(
                        f'CREATE VIEW "{safe_name}" AS '
                        f'SELECT * FROM {alias}."{safe_name}"'
                    )
                    attached += 1
                except Exception as e:
                    # A single broken artifact must not take down the whole
                    # session — the other relations still serve.
                    logger.error(
                        "fast.connect.attach_failed",
                        extra={"relation": rel.name, "error": str(e)},
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
            lines.append(
                f"- `{rel.name}`{desc}: {rel.row_count:,} rows{freshness}. Columns: {cols}"
            )
        lines += [
            "",
            "These relations are refreshed on a schedule, so figures reflect the",
            "'as of' time shown above rather than this instant. Say so when the",
            "recency of the answer matters.",
        ]
        return "\n".join(lines)

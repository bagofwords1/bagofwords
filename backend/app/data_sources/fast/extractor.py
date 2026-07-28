"""Bounded, streaming extraction of a custom query into an encrypted artifact.

A custom query is the one place in the product where a deliberately huge scan
can happen — an admin can write `SELECT * FROM orders` against a 2-billion-row
table. Three independent layers stop that taking the process down:

1. **Refuse before running.** `EXPLAIN` gives an estimated row count and row
   width without executing. Checked at save time and before every refresh.
2. **Never materialize.** Rows stream from a server-side cursor in batches and
   are appended straight into the DuckDB artifact. Peak memory is O(batch),
   independent of result size.
3. **Hard caps with graceful abort.** Row / byte / wall-clock ceilings. On
   breach the partial artifact is discarded and the previous one keeps serving.

This is a NEW extraction path, deliberately not a change to
`DataSourceClient.execute_query` — that returns a full DataFrame by contract and
every existing caller depends on it.
"""

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pyarrow as pa
import sqlalchemy
from sqlalchemy import text

from app.data_sources.fast import artifacts

logger = logging.getLogger(__name__)

# Defaults. Overridable per-connection via config later; conservative enough
# that a careless SELECT * is refused rather than absorbed.
DEFAULT_MAX_ROWS = 5_000_000
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024      # 2 GiB estimated source bytes
DEFAULT_MAX_SECONDS = 1800                       # 30 min per refresh
BATCH_ROWS = 50_000

# The preview shown in the authoring modal is always bounded, no matter what the
# admin typed. Never run their raw SQL unbounded from a UI action.
PREVIEW_ROW_LIMIT = 100


class ExtractionRefused(Exception):
    """Raised before execution when the estimated result exceeds a budget."""


class ExtractionAborted(Exception):
    """Raised mid-flight when a hard cap is breached."""


@dataclass
class Estimate:
    rows: Optional[int] = None
    width_bytes: Optional[int] = None
    total_bytes: Optional[int] = None
    supported: bool = True
    note: str = ""


@dataclass
class ExtractResult:
    row_count: int = 0
    columns: list = field(default_factory=list)
    artifact_path: str = ""
    artifact_key: str = ""
    artifact_bytes: int = 0
    elapsed_ms: int = 0


def _sqlalchemy_engine(client) -> Optional[sqlalchemy.engine.Engine]:
    """Build an engine for clients that expose a SQLAlchemy URI.

    Only clients we have explicitly verified for streaming are supported; every
    other client falls back to the bounded non-streaming path.
    """
    for attr in ("pg_uri", "mysql_uri", "mariadb_uri", "sql_server_uri", "oracle_uri"):
        uri = getattr(client, attr, None)
        if uri:
            return sqlalchemy.create_engine(uri)
    return None


def estimate(client, sql: str) -> Estimate:
    """Estimated rows/bytes for `sql` WITHOUT executing it.

    Postgres and MySQL/MariaDB both support `EXPLAIN`; the shapes differ, so
    each is parsed separately. Anything else returns `supported=False` and the
    caller falls back to hard caps alone.
    """
    engine = _sqlalchemy_engine(client)
    if engine is None:
        return Estimate(supported=False, note="no SQLAlchemy URI on this client")

    try:
        with engine.connect() as conn:
            # --- Postgres ---------------------------------------------------
            if getattr(client, "pg_uri", None):
                row = conn.execute(
                    text(f"EXPLAIN (FORMAT JSON) {sql}")
                ).scalar()
                plan = row[0]["Plan"] if isinstance(row, list) else row["Plan"]
                rows = int(plan.get("Plan Rows") or 0)
                width = int(plan.get("Plan Width") or 0)
                return Estimate(
                    rows=rows, width_bytes=width, total_bytes=rows * max(width, 1)
                )

            # --- MySQL / MariaDB --------------------------------------------
            res = conn.execute(text(f"EXPLAIN {sql}")).mappings().all()
            rows = 0
            for r in res:
                v = r.get("rows") or r.get("ROWS")
                if v:
                    rows = max(rows, int(v))
            # EXPLAIN gives no width; assume a modest row width so the byte
            # ceiling still has something to bite on.
            width = 100
            return Estimate(
                rows=rows, width_bytes=width, total_bytes=rows * width,
                note="row width estimated (EXPLAIN provides none)",
            )
    except Exception as e:
        return Estimate(supported=False, note=f"EXPLAIN failed: {e}")
    finally:
        engine.dispose()


def check_budget(
    est: Estimate,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> None:
    """Raise ExtractionRefused when an estimate blows a budget."""
    if not est.supported:
        return
    if est.rows and est.rows > max_rows:
        raise ExtractionRefused(
            f"Estimated {est.rows:,} rows exceeds the {max_rows:,}-row limit for a "
            f"custom query. Narrow the query with a WHERE clause or fewer columns."
        )
    if est.total_bytes and est.total_bytes > max_bytes:
        raise ExtractionRefused(
            f"Estimated {est.total_bytes / (1024**3):.1f} GB exceeds the "
            f"{max_bytes / (1024**3):.1f} GB limit for a custom query. "
            f"Narrow the query with a WHERE clause or fewer columns."
        )


def preview(client, sql: str, limit: int = PREVIEW_ROW_LIMIT) -> tuple[list, list]:
    """Run `sql` bounded to `limit` rows. Returns (columns, rows).

    The admin's SQL is wrapped rather than trusted to carry its own LIMIT — the
    modal must never issue an unbounded query.
    """
    engine = _sqlalchemy_engine(client)
    wrapped = f"SELECT * FROM ({sql.rstrip().rstrip(';')}) AS bow_preview LIMIT {int(limit)}"

    if engine is None:
        # Fall back to the client's own execute_query, then truncate. Still
        # bounded, just less efficiently.
        df = client.execute_query(wrapped)
        cols = [{"name": c, "dtype": str(df[c].dtype)} for c in df.columns]
        return cols, df.head(limit).astype(object).where(df.notna(), None).values.tolist()

    try:
        with engine.connect() as conn:
            result = conn.execute(text(wrapped))
            colnames = list(result.keys())
            rows = [list(r) for r in result.fetchall()]
        cols = [{"name": c, "dtype": None} for c in colnames]
        return cols, rows
    finally:
        engine.dispose()


def _arrow_type_name(t: pa.DataType) -> str:
    return str(t)


def extract_to_artifact(
    client,
    sql: str,
    relation_name: str,
    connection_id: str,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_seconds: int = DEFAULT_MAX_SECONDS,
) -> ExtractResult:
    """Stream `sql` into a fresh encrypted DuckDB artifact.

    Writes to a `.tmp` sibling and atomically renames on success, so a crashed
    or aborted refresh can never leave a half-written artifact in place. The
    caller keeps serving the previous artifact until the swap lands.
    """
    started = time.monotonic()
    engine = _sqlalchemy_engine(client)
    key = artifacts.new_artifact_key()
    final_path = artifacts.new_artifact_path(connection_id)
    tmp_path = final_path.with_suffix(".tmp")

    row_count = 0
    columns: list = []
    con = None

    def _elapsed_guard():
        if time.monotonic() - started > max_seconds:
            raise ExtractionAborted(
                f"Refresh exceeded the {max_seconds}s limit and was aborted; "
                f"the previous data is still being served."
            )

    try:
        con = artifacts.connect_encrypted(tmp_path, key)
        safe_rel = relation_name.replace('"', '""')

        if engine is not None:
            # --- streaming path -------------------------------------------
            with engine.connect().execution_options(
                stream_results=True, yield_per=BATCH_ROWS
            ) as conn:
                result = conn.execute(text(sql))
                colnames = list(result.keys())
                first = True
                while True:
                    _elapsed_guard()
                    batch = result.fetchmany(BATCH_ROWS)
                    if not batch:
                        break
                    row_count += len(batch)
                    if row_count > max_rows:
                        raise ExtractionAborted(
                            f"Query returned more than the {max_rows:,}-row limit "
                            f"for a custom query and was aborted."
                        )
                    tbl = pa.Table.from_pydict(
                        {
                            name: [r[i] for r in batch]
                            for i, name in enumerate(colnames)
                        }
                    )
                    con.register("bow_batch", tbl)
                    if first:
                        con.execute(
                            f'CREATE TABLE "{safe_rel}" AS SELECT * FROM bow_batch'
                        )
                        columns = [
                            {"name": f.name, "dtype": _arrow_type_name(f.type)}
                            for f in tbl.schema
                        ]
                        first = False
                    else:
                        con.execute(
                            f'INSERT INTO "{safe_rel}" SELECT * FROM bow_batch'
                        )
                    con.unregister("bow_batch")

                    if artifacts.artifact_size(str(tmp_path)) > max_bytes:
                        raise ExtractionAborted(
                            f"Artifact exceeded the "
                            f"{max_bytes / (1024**3):.1f} GB limit and was aborted."
                        )

                if first:
                    # Zero rows: still create the relation so the shape exists.
                    empty = pa.Table.from_pydict({c: [] for c in colnames})
                    con.register("bow_batch", empty)
                    con.execute(
                        f'CREATE TABLE "{safe_rel}" AS SELECT * FROM bow_batch'
                    )
                    columns = [
                        {"name": f.name, "dtype": _arrow_type_name(f.type)}
                        for f in empty.schema
                    ]
                    con.unregister("bow_batch")
        else:
            # --- fallback for clients without a SQLAlchemy URI --------------
            # Bounded by an injected LIMIT so a non-streaming client still
            # cannot pull an unbounded result into memory.
            bounded = (
                f"SELECT * FROM ({sql.rstrip().rstrip(';')}) AS bow_x "
                f"LIMIT {int(max_rows)}"
            )
            df = client.execute_query(bounded)
            row_count = len(df)
            tbl = pa.Table.from_pandas(df, preserve_index=False)
            con.register("bow_batch", tbl)
            con.execute(f'CREATE TABLE "{safe_rel}" AS SELECT * FROM bow_batch')
            con.unregister("bow_batch")
            columns = [
                {"name": f.name, "dtype": _arrow_type_name(f.type)}
                for f in tbl.schema
            ]

        con.close()
        con = None
        tmp_path.replace(final_path)

        return ExtractResult(
            row_count=row_count,
            columns=columns,
            artifact_path=str(final_path),
            artifact_key=key,
            artifact_bytes=artifacts.artifact_size(str(final_path)),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    except BaseException:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        artifacts.delete_artifact(str(tmp_path))
        raise
    finally:
        if engine is not None:
            engine.dispose()

from __future__ import annotations

import glob
import os
import re
import time
from contextlib import contextmanager
from typing import Generator, List, Optional

import duckdb
import pandas as pd

from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import ProgressCallback, make_reporter
from app.settings.logging_config import get_logger


logger = get_logger(__name__)


class CSVClient(DataSourceClient):
    """Read CSV files and query them via SQL using DuckDB.

    Unlike QVD (a proprietary binary format that must be converted first), CSV is
    read natively by DuckDB via ``read_csv_auto``. So there is no conversion,
    cache, or warmup layer here — each resolved file is exposed directly as an
    in-memory DuckDB view at connect time. One file → one table.
    """

    def __init__(
        self,
        file_paths: str,
        delimiter: str = "",
        has_header: bool = True,
        encoding: str = "utf-8",
    ):
        """
        Args:
            file_paths: Newline-separated list of file paths or glob patterns.
                        e.g., "/data/*.csv" or "/data/Sales.csv\n/data/Products.csv"
            delimiter:  Column delimiter. Empty string means auto-detect.
            has_header: Whether the first row holds column names.
            encoding:   File encoding (e.g. utf-8, latin-1).
        """
        self.file_paths_raw = file_paths or ""
        self.patterns: List[str] = [
            p.strip() for p in self.file_paths_raw.splitlines() if p.strip()
        ]
        self.delimiter = delimiter or ""
        self.has_header = has_header if has_header is not None else True
        self.encoding = encoding or "utf-8"
        self._table_map: dict[str, str] = {}

    def _resolve_files(self) -> List[str]:
        """Expand glob patterns to actual file paths."""
        files = []
        for pattern in self.patterns:
            matched = glob.glob(pattern, recursive=True)
            files.extend([f for f in matched if f.lower().endswith('.csv')])
        return sorted(set(files))

    def _safe_table_name(self, filepath: str, used: set[str]) -> str:
        """Generate a safe table name from filepath."""
        basename = os.path.splitext(os.path.basename(filepath))[0]
        name = re.sub(r"[^a-zA-Z0-9_]+", "_", basename).strip("_").lower() or "table"
        original = name
        i = 1
        while name in used:
            i += 1
            name = f"{original}_{i}"
        used.add(name)
        return name

    @staticmethod
    def _sql_str(value: str) -> str:
        """Single-quote a value for inlining in SQL, escaping embedded quotes."""
        return "'" + str(value).replace("'", "''") + "'"

    def _read_expr(self, filepath: str) -> str:
        """Build the ``read_csv_auto(...)`` expression used for BOTH schema
        discovery and query execution, so the two can never disagree on types.
        """
        opts: List[str] = [self._sql_str(filepath)]
        opts.append(f"header={'true' if self.has_header else 'false'}")
        if self.delimiter:
            # Allow the literal two-char sequence "\t" from the form to mean a tab.
            delim = "\t" if self.delimiter in ("\\t", "\t") else self.delimiter
            opts.append(f"delim={self._sql_str(delim)}")
        if self.encoding:
            opts.append(f"encoding={self._sql_str(self.encoding)}")
        return f"read_csv_auto({', '.join(opts)})"

    @staticmethod
    def _normalize_duckdb_type(dtype: str) -> str:
        """Strip precision suffix so TIMESTAMP_NS/_MS/_S display as TIMESTAMP."""
        if dtype.startswith("TIMESTAMP_"):
            return "TIMESTAMP"
        return dtype

    def _describe_file(self, con: duckdb.DuckDBPyConnection, filepath: str) -> List[tuple[str, str]]:
        """Schema for a single CSV via DuckDB DESCRIBE over the read expression."""
        rows = con.execute(f"DESCRIBE SELECT * FROM {self._read_expr(filepath)}").fetchall()
        return [(r[0], self._normalize_duckdb_type(str(r[1]))) for r in rows]

    @contextmanager
    def connect(self) -> Generator[duckdb.DuckDBPyConnection, None, None]:
        t0 = time.perf_counter()
        files = self._resolve_files()
        logger.info(
            "csv.connect.start",
            extra={"csv_patterns": self.patterns, "csv_files_found": len(files)},
        )
        con: duckdb.DuckDBPyConnection | None = None
        yielded = False
        try:
            con = duckdb.connect(database=":memory:")
            used: set[str] = set()
            table_map: dict[str, str] = {}
            for filepath in files:
                table_name = self._safe_table_name(filepath, used)
                con.execute(
                    f"CREATE VIEW {table_name} AS SELECT * FROM {self._read_expr(filepath)}"
                )
                table_map[table_name] = filepath

            self._table_map = table_map
            logger.info(
                "csv.connect.done",
                extra={
                    "csv_tables": list(table_map.keys()),
                    "csv_elapsed_s": round(time.perf_counter() - t0, 3),
                },
            )
            yielded = True
            yield con
        except Exception as e:
            # Exceptions thrown by work inside the context belong to the query
            # layer (not connection setup). Preserve typed domain errors such as
            # ResultTooLargeError instead of wrapping them as connection errors.
            if yielded:
                raise
            logger.error(
                "csv.connect.error",
                extra={"csv_error": str(e), "csv_elapsed_s": round(time.perf_counter() - t0, 3)},
            )
            raise RuntimeError(f"Error connecting to CSV files: {e}") from e
        finally:
            if con is not None:
                con.close()

    def execute_query(self, sql: str) -> pd.DataFrame:
        t0 = time.perf_counter()
        logger.info("csv.query.start", extra={"csv_sql": sql})
        try:
            with self.connect() as con:
                df = con.execute(sql).df()
            logger.info(
                "csv.query.done",
                extra={
                    "csv_sql": sql,
                    "csv_rows": len(df),
                    "csv_cols": len(df.columns),
                    "csv_elapsed_s": round(time.perf_counter() - t0, 3),
                },
            )
            return df
        except Exception as exc:
            logger.error(
                "csv.query.error",
                extra={
                    "csv_sql": sql,
                    "csv_error": str(exc),
                    "csv_elapsed_s": round(time.perf_counter() - t0, 3),
                },
            )
            raise

    # Streaming lazy path (out-of-core, v2) — dispatched by the base class;
    # see DataSourceClient.execute_query_lazy. connect() yields a DuckDB
    # connection with views over the CSVs, so the native COPY streamer applies.
    _lazy_strategy = "duckdb"

    def get_tables(self, progress_callback: Optional[ProgressCallback] = None) -> List[Table]:
        """Schema lookup via DuckDB DESCRIBE over each CSV's read expression —
        the same expression used at query time, so types are ground truth.
        """
        tables: List[Table] = []
        used: set[str] = set()
        files = self._resolve_files()
        logger.debug("csv.schema.start", extra={"csv_files": len(files)})
        reporter = make_reporter(progress_callback)
        reporter.phase("csv_files", total=len(files))
        con = duckdb.connect(database=":memory:")
        try:
            for filepath in files:
                reporter.item(os.path.basename(filepath))
                table_name = self._safe_table_name(filepath, used)
                cols: List[TableColumn] = []
                try:
                    for fname, ftype in self._describe_file(con, filepath):
                        cols.append(TableColumn(name=fname, dtype=ftype))
                except Exception as exc:
                    logger.warning(
                        "csv.schema.file.error",
                        extra={"csv_file": filepath, "csv_error": str(exc)},
                    )
                tables.append(Table(
                    name=table_name,
                    columns=cols,
                    pks=[],
                    fks=[],
                    metadata_json={"csv": {"source_file": filepath}}
                ))
        finally:
            con.close()
        reporter.done()
        logger.debug("csv.schema.done", extra={"csv_tables": len(tables)})
        return tables

    def get_schemas(self, progress_callback: Optional[ProgressCallback] = None) -> List[Table]:
        return self.get_tables(progress_callback=progress_callback)

    def get_schema(self, table_name: str) -> Table:
        for t in self.get_tables():
            if t.name == table_name:
                return t
        return Table(name=table_name, columns=[], pks=[], fks=[], metadata_json={})

    def prompt_schema(self) -> str:
        return TableFormatter(self.get_schemas()).table_str

    def test_connection(self) -> dict:
        """Lightweight check: verifies files exist and are parseable. No full load."""
        try:
            files = self._resolve_files()
            if not files:
                return {
                    "success": False,
                    "message": "No CSV files found matching the patterns",
                    "details": {"files_found": 0, "patterns": self.patterns},
                }
            total_bytes = 0
            con = duckdb.connect(database=":memory:")
            try:
                for f in files:
                    try:
                        total_bytes += os.path.getsize(f)
                    except OSError:
                        pass
                    # Header-only sniff — DESCRIBE reads just enough to type columns.
                    self._describe_file(con, f)
            finally:
                con.close()
            return {
                "success": True,
                "message": f"Successfully verified {len(files)} CSV file(s)",
                "details": {
                    "files_found": len(files),
                    "total_bytes": total_bytes,
                    "sample_files": [os.path.basename(f) for f in files[:5]],
                },
            }
        except Exception as e:
            return {"success": False, "message": str(e), "details": {}}

    @property
    def description(self) -> str:
        files = self._resolve_files()
        sample = ", ".join([os.path.basename(f) for f in files[:3]])
        if len(files) > 3:
            sample += ", ..."

        return f"""CSV files: {sample}

You can query these files using SQL (DuckDB syntax). Each file is exposed as a table.

Examples:
```python
df = client.execute_query("SELECT * FROM sales LIMIT 10")
```
```python
df = client.execute_query("SELECT product, SUM(amount) AS total FROM sales GROUP BY product")
```
"""


CsvClient = CSVClient

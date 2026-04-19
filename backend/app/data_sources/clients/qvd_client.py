from __future__ import annotations

import asyncio
import glob
import hashlib
import os
import re
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, List

import duckdb
import pandas as pd

from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
from app.data_sources.clients.base import DataSourceClient
from app.settings.logging_config import get_logger


logger = get_logger(__name__)

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "uploads" / "qvd_cache"
_HEADER_END_TAG = b"</QvdTableHeader>"
_HEADER_SCAN_LIMIT = 4 * 1024 * 1024

# Cross-instance coordination for warmup. Same (abspath, mtime) → one parse.
# Lazily initialized on first use so we bind to the running event loop.
_INFLIGHT: dict[Path, "asyncio.Task[Path]"] = {}
_INFLIGHT_LOCK: asyncio.Lock | None = None
_WARMUP_SEMAPHORE: asyncio.Semaphore | None = None


def _get_inflight_lock() -> asyncio.Lock:
    global _INFLIGHT_LOCK
    if _INFLIGHT_LOCK is None:
        _INFLIGHT_LOCK = asyncio.Lock()
    return _INFLIGHT_LOCK


def _get_warmup_semaphore() -> asyncio.Semaphore:
    # A 5GB QVD → ~20GB RAM during pyqvd parse; one at a time per pod.
    global _WARMUP_SEMAPHORE
    if _WARMUP_SEMAPHORE is None:
        _WARMUP_SEMAPHORE = asyncio.Semaphore(1)
    return _WARMUP_SEMAPHORE


class QVDClient(DataSourceClient):
    """Read QVD (QlikView Data) files and query them via SQL using DuckDB."""

    def __init__(self, file_paths: str):
        """
        Args:
            file_paths: Newline-separated list of file paths or glob patterns.
                        e.g., "/data/*.qvd" or "/data/Sales.qvd\n/data/Products.qvd"
        """
        self.file_paths_raw = file_paths or ""
        self.patterns: List[str] = [
            p.strip() for p in self.file_paths_raw.splitlines() if p.strip()
        ]
        self._table_map: dict[str, str] = {}

    def _resolve_files(self) -> List[str]:
        """Expand glob patterns to actual file paths."""
        files = []
        for pattern in self.patterns:
            matched = glob.glob(pattern, recursive=True)
            files.extend([f for f in matched if f.lower().endswith('.qvd')])
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
    def _infer_duckdb_type(tags: List[str]) -> str:
        """Map QVD <Tags> to the DuckDB type the Parquet cache will expose."""
        s = set(tags)
        if "$timestamp" in s:
            return "TIMESTAMP"
        if "$date" in s:
            return "DATE"
        if "$integer" in s:
            return "BIGINT"
        if "$numeric" in s:
            return "DOUBLE"
        return "VARCHAR"

    @classmethod
    def _read_qvd_header(cls, filepath: str) -> List[tuple[str, str]]:
        """Parse QVD XML header without loading data. Returns [(field_name, duckdb_type), ...]."""
        with open(filepath, "rb") as f:
            buf = f.read(_HEADER_SCAN_LIMIT)
        idx = buf.find(_HEADER_END_TAG)
        if idx == -1:
            raise RuntimeError(f"Not a valid QVD file (header not found): {filepath}")
        xml_str = buf[: idx + len(_HEADER_END_TAG)].decode("utf-8", errors="replace")
        root = ET.fromstring(xml_str)
        fields: List[tuple[str, str]] = []
        for fd in root.findall("./Fields/QvdFieldHeader"):
            name = (fd.findtext("FieldName") or "").strip()
            tags = [t.text for t in fd.findall("./Tags/String") if t.text]
            if name:
                fields.append((name, cls._infer_duckdb_type(tags)))
        return fields

    @classmethod
    def _describe_parquet(cls, parquet_path: Path) -> List[tuple[str, str]]:
        """Ground-truth schema from DuckDB over the cached Parquet."""
        con = duckdb.connect(database=":memory:")
        try:
            path_sql = str(parquet_path).replace("'", "''")
            rows = con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{path_sql}')"
            ).fetchall()
            return [(r[0], cls._normalize_duckdb_type(str(r[1]))) for r in rows]
        finally:
            con.close()

    @staticmethod
    def _normalize_duckdb_type(dtype: str) -> str:
        """Strip precision suffix so TIMESTAMP_NS/_MS/_S display as TIMESTAMP."""
        if dtype.startswith("TIMESTAMP_"):
            return "TIMESTAMP"
        return dtype

    @staticmethod
    def _cache_key(filepath: str) -> tuple[str, Path]:
        """Return (file_hash, cache_path) for the given QVD file + its current mtime."""
        abs_path = os.path.abspath(filepath)
        file_hash = hashlib.sha1(abs_path.encode("utf-8")).hexdigest()[:16]
        mtime_ns = os.stat(abs_path).st_mtime_ns
        return file_hash, _CACHE_DIR / f"{file_hash}_{mtime_ns}.parquet"

    def _ensure_parquet(self, filepath: str) -> Path:
        """Return cached Parquet path, parsing QVD if cache is stale/missing."""
        file_hash, cache_path = self._cache_key(filepath)
        if cache_path.exists():
            return cache_path

        try:
            from pyqvd import QvdTable
        except ImportError:
            raise ImportError(
                "The 'pyqvd' package is required for QVD support. "
                "Install it with: pip install pyqvd"
            )

        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
        df = QvdTable.from_qvd(filepath).to_pandas()
        # Object-dtype columns from QVD can contain mixed Python types
        # (numbers alongside strings). pyarrow's parquet writer rejects those;
        # coercing to pandas StringDtype produces VARCHAR in DuckDB either way.
        obj_cols = df.select_dtypes(include=["object"]).columns
        if len(obj_cols):
            df[obj_cols] = df[obj_cols].astype("string")
        df.to_parquet(tmp_path, index=False)
        os.replace(tmp_path, cache_path)

        for old in _CACHE_DIR.glob(f"{file_hash}_*.parquet"):
            if old != cache_path:
                try:
                    old.unlink()
                except OSError:
                    pass
        return cache_path

    async def _warm_one(self, filepath: str) -> Path:
        """Parse one QVD to Parquet under the pod-wide warmup semaphore."""
        sem = _get_warmup_semaphore()
        async with sem:
            _, cache_path = self._cache_key(filepath)
            if cache_path.exists():
                return cache_path
            try:
                size = os.path.getsize(filepath)
            except OSError:
                size = -1
            t0 = time.perf_counter()
            logger.info(
                "qvd.warm.start",
                extra={"qvd_file": filepath, "qvd_bytes": size},
            )
            try:
                result = await asyncio.to_thread(self._ensure_parquet, filepath)
            except Exception as exc:
                logger.exception(
                    "qvd.warm.failed",
                    extra={
                        "qvd_file": filepath,
                        "qvd_elapsed_s": round(time.perf_counter() - t0, 2),
                        "qvd_error": str(exc),
                    },
                )
                raise
            logger.info(
                "qvd.warm.parquet_done",
                extra={
                    "qvd_file": filepath,
                    "qvd_cache": str(result),
                    "qvd_elapsed_s": round(time.perf_counter() - t0, 2),
                },
            )
            return result

    async def aensure_warm(self, filepath: str) -> Path:
        """
        Ensure the Parquet cache for `filepath` is populated.
        Deduplicates in-flight parses by (abspath, mtime) so concurrent callers
        share one parse task.
        """
        _, cache_path = self._cache_key(filepath)
        if cache_path.exists():
            return cache_path

        lock = _get_inflight_lock()
        async with lock:
            task = _INFLIGHT.get(cache_path)
            if task is None or task.done():
                task = asyncio.create_task(self._warm_one(filepath))
                _INFLIGHT[cache_path] = task

                def _cleanup(t: "asyncio.Task[Path]", key: Path = cache_path) -> None:
                    # Only drop the entry if it's still ours (mtime may have moved on).
                    if _INFLIGHT.get(key) is t:
                        _INFLIGHT.pop(key, None)

                task.add_done_callback(_cleanup)
        return await task

    async def awarm_all(self) -> List[Path]:
        """Warm every resolved QVD file. Errors on individual files are logged, not raised."""
        paths: List[Path] = []
        for filepath in self._resolve_files():
            try:
                paths.append(await self.aensure_warm(filepath))
            except Exception:
                # _warm_one already logged qvd.warm.failed
                continue
        return paths

    @contextmanager
    def connect(self) -> Generator[duckdb.DuckDBPyConnection, None, None]:
        con: duckdb.DuckDBPyConnection | None = None
        try:
            con = duckdb.connect(database=":memory:")
            used: set[str] = set()
            table_map: dict[str, str] = {}
            for filepath in self._resolve_files():
                parquet = self._ensure_parquet(filepath)
                table_name = self._safe_table_name(filepath, used)
                parquet_sql = str(parquet).replace("'", "''")
                con.execute(
                    f"CREATE VIEW {table_name} AS SELECT * FROM read_parquet('{parquet_sql}')"
                )
                table_map[table_name] = filepath
            self._table_map = table_map
            yield con
        except Exception as e:
            raise RuntimeError(f"Error connecting to QVD files: {e}")
        finally:
            if con is not None:
                con.close()

    def execute_query(self, sql: str) -> pd.DataFrame:
        with self.connect() as con:
            return con.execute(sql).df()

    def get_tables(self) -> List[Table]:
        """
        Schema lookup. Uses DuckDB DESCRIBE on the cached Parquet when available
        (ground truth for queries); otherwise falls back to the QVD XML header.
        Both paths return DuckDB-compatible type names.
        """
        tables: List[Table] = []
        used: set[str] = set()
        for filepath in self._resolve_files():
            table_name = self._safe_table_name(filepath, used)
            cols: List[TableColumn] = []
            try:
                _, cache_path = self._cache_key(filepath)
                if cache_path.exists():
                    fields = self._describe_parquet(cache_path)
                else:
                    fields = self._read_qvd_header(filepath)
                for fname, ftype in fields:
                    cols.append(TableColumn(name=fname, dtype=ftype))
            except Exception:
                pass
            tables.append(Table(
                name=table_name,
                columns=cols,
                pks=[],
                fks=[],
                metadata_json={"qvd": {"source_file": filepath}}
            ))
        return tables

    def get_schemas(self) -> List[Table]:
        return self.get_tables()

    def get_schema(self, table_name: str) -> Table:
        for t in self.get_tables():
            if t.name == table_name:
                return t
        return Table(name=table_name, columns=[], pks=[], fks=[], metadata_json={})

    def prompt_schema(self) -> str:
        return TableFormatter(self.get_schemas()).table_str

    def test_connection(self) -> dict:
        """Lightweight check: verifies files exist and headers are valid. No data load."""
        try:
            files = self._resolve_files()
            if not files:
                return {
                    "success": False,
                    "message": "No QVD files found matching the patterns"
                }
            for f in files:
                self._read_qvd_header(f)
            return {
                "success": True,
                "message": f"Successfully verified {len(files)} QVD file(s)"
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

    @property
    def description(self) -> str:
        files = self._resolve_files()
        sample = ", ".join([os.path.basename(f) for f in files[:3]])
        if len(files) > 3:
            sample += ", ..."

        return f"""QVD files: {sample}

You can query these files using SQL (DuckDB syntax).

Examples:
```python
df = client.execute_query("SELECT * FROM sales LIMIT 10")
```
```python
df = client.execute_query("SELECT product, SUM(amount) AS total FROM sales GROUP BY product")
```
"""


QvdClient = QVDClient


async def warm_all_qvd_caches() -> None:
    """
    Scheduled maintenance: walk every active QVD Connection and ensure its
    Parquet caches are warm. Designed to run on an APScheduler interval.
    A single module-level semaphore caps concurrent pyqvd parses at 1 per pod.
    """
    from sqlalchemy import select

    from app.dependencies import async_session_maker
    from app.models.connection import Connection

    t0 = time.perf_counter()
    async with async_session_maker() as db:
        rows = (
            await db.execute(
                select(Connection).where(
                    Connection.type == "qvd",
                    Connection.is_active.is_(True),
                    Connection.deleted_at.is_(None),
                )
            )
        ).scalars().all()

    if not rows:
        logger.info("qvd.warmup.sweep", extra={"qvd_connections": 0})
        return

    logger.info("qvd.warmup.sweep.start", extra={"qvd_connections": len(rows)})
    warmed = 0
    failed = 0
    for conn in rows:
        try:
            client = conn.get_client()
        except Exception as exc:
            logger.warning(
                "qvd.warmup.client_init_failed",
                extra={"connection_id": str(conn.id), "qvd_error": str(exc)},
            )
            failed += 1
            continue
        if not isinstance(client, QVDClient):
            continue
        try:
            await client.awarm_all()
            warmed += 1
        except Exception as exc:
            logger.warning(
                "qvd.warmup.connection_failed",
                extra={"connection_id": str(conn.id), "qvd_error": str(exc)},
            )
            failed += 1

    logger.info(
        "qvd.warmup.sweep.done",
        extra={
            "qvd_connections": len(rows),
            "qvd_warmed": warmed,
            "qvd_failed": failed,
            "qvd_elapsed_s": round(time.perf_counter() - t0, 2),
        },
    )

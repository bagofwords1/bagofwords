from __future__ import annotations

import asyncio
import glob
import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
from defusedxml import ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
import select as _select_mod
from typing import Callable, Generator, List, Optional

import duckdb
import pandas as pd

from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import (
    CancelCheck,
    IndexingCancelled,
    ProgressCallback,
    make_reporter,
)
from app.settings.logging_config import get_logger


logger = get_logger(__name__)

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "uploads" / "qvd_cache"
# Bumped whenever the qvd2parquet converter changes the Parquet schema, so old
# caches are treated as missing and reconverted instead of served stale.
#   v2: temporal columns ($date/$timestamp/$time) emitted as DATE/TIMESTAMP/TIME
#       instead of raw Excel-style serial numbers.
_PARQUET_SCHEMA_VERSION = 2
_HEADER_END_TAG = b"</QvdTableHeader>"
_HEADER_SCAN_LIMIT = 4 * 1024 * 1024

# Resolved once at import. Override with QVD2PARQUET_BIN for dev/test.
# Validate strictly: an env-supplied override must be an absolute path to an
# existing regular file so the binary location can't be swapped to an arbitrary
# program (Snyk python/CommandInjection — env input flowing into subprocess.run).
def _resolve_qvd2parquet_bin() -> str:
    override = os.environ.get("QVD2PARQUET_BIN")
    if override:
        if not re.fullmatch(r"[A-Za-z0-9_./\-]+", override) or not os.path.isabs(override):
            raise RuntimeError(
                f"QVD2PARQUET_BIN must be an absolute path with no shell metacharacters: {override!r}"
            )
        resolved = os.path.realpath(override)
        if not os.path.isfile(resolved):
            raise RuntimeError(f"QVD2PARQUET_BIN points to a non-existent file: {override!r}")
        return resolved
    return shutil.which("qvd2parquet") or "/usr/local/bin/qvd2parquet"


_QVD2PARQUET_BIN = _resolve_qvd2parquet_bin()

# Per-cache-path lock so concurrent threads (connect() calls) don't race writing
# the same .parquet.tmp file. Keyed by the final cache_path (Path).
_PARSE_LOCKS: dict[Path, threading.Lock] = {}
_PARSE_LOCKS_MUTEX = threading.Lock()

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
    # qvd2parquet streams; RAM bounded by symbol tables + chunk buffer.
    # We still cap concurrency at 1 per pod since parse is disk+CPU bound
    # and parallel warmups would just thrash each other.
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
    def _read_qvd_record_count(cls, filepath: str) -> int:
        """Parse the row count from the QVD XML header (<NoOfRecords>) without
        loading data. Returns 0 when the tag is absent or unparseable — callers
        treat 0 as "unknown total" and fall back to an indeterminate bar.
        """
        try:
            with open(filepath, "rb") as f:
                buf = f.read(_HEADER_SCAN_LIMIT)
            idx = buf.find(_HEADER_END_TAG)
            if idx == -1:
                return 0
            xml_str = buf[: idx + len(_HEADER_END_TAG)].decode("utf-8", errors="replace")
            root = ET.fromstring(xml_str)
            text = root.findtext("NoOfRecords")
            return int(text) if text and text.strip().isdigit() else 0
        except Exception:
            return 0

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
        file_hash = hashlib.sha256(abs_path.encode("utf-8")).hexdigest()[:16]
        mtime_ns = os.stat(abs_path).st_mtime_ns
        return file_hash, _CACHE_DIR / f"{file_hash}_{mtime_ns}_v{_PARQUET_SCHEMA_VERSION}.parquet"

    @staticmethod
    def _find_any_parquet(file_hash: str) -> Path | None:
        """Return the newest complete parquet for this file hash, any mtime.
        Scoped to the current schema version so a stale (source changed) cache is
        still served, but a parquet written by an older converter schema is not.
        """
        candidates = sorted(
            _CACHE_DIR.glob(f"{file_hash}_*_v{_PARQUET_SCHEMA_VERSION}.parquet"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return candidates[0] if candidates else None

    # Machine-parseable progress line the Rust converter writes to stderr:
    #   "qvd2parquet: progress <done_rows> <total_rows>"
    _PROGRESS_RE = re.compile(r"progress\s+(\d+)\s+(\d+)")

    def _run_convert_streaming(
        self,
        filepath: str,
        tmp_path: Path,
        *,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        cancel_check: Optional[CancelCheck] = None,
        timeout: float = 3600.0,
    ) -> str:
        """Run qvd2parquet as a child process, streaming its stderr so we can
        (a) forward row-level progress to `progress_cb(done_rows, total_rows)`
        and (b) kill it promptly when `cancel_check()` flips to True.

        Returns the collected stderr text. Raises RuntimeError on non-zero exit
        or timeout, and IndexingCancelled when cancelled mid-convert.
        """
        try:
            proc = subprocess.Popen(
                [_QVD2PARQUET_BIN, filepath, str(tmp_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"qvd2parquet binary not found at {_QVD2PARQUET_BIN}. "
                "Set QVD2PARQUET_BIN or install the binary at /usr/local/bin/qvd2parquet."
            ) from exc

        stderr_lines: list[str] = []
        deadline = time.perf_counter() + timeout
        cancelled = False
        try:
            # Poll stderr with a short timeout so we still notice a cancel
            # request even while the converter is churning a slow chunk.
            while True:
                if cancel_check is not None and cancel_check():
                    cancelled = True
                    break
                if time.perf_counter() > deadline:
                    proc.kill()
                    proc.wait(timeout=10)
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise RuntimeError(
                        f"qvd2parquet timed out after {timeout}s on {filepath}"
                    )
                ready, _, _ = _select_mod.select([proc.stderr], [], [], 0.25)
                if ready:
                    line = proc.stderr.readline()
                    if line == "":
                        break  # EOF — process is finishing
                    stderr_lines.append(line)
                    if progress_cb is not None:
                        m = self._PROGRESS_RE.search(line)
                        if m:
                            try:
                                progress_cb(int(m.group(1)), int(m.group(2)))
                            except Exception:
                                pass
                elif proc.poll() is not None:
                    # No pending output and the process has exited — drain and stop.
                    rest = proc.stderr.read()
                    if rest:
                        stderr_lines.append(rest)
                    break
        finally:
            if proc.stderr is not None:
                try:
                    proc.stderr.close()
                except Exception:
                    pass

        if cancelled:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise IndexingCancelled(f"QVD conversion cancelled: {filepath}")

        returncode = proc.wait()
        stderr = "".join(stderr_lines)
        if returncode != 0:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError(
                f"qvd2parquet failed on {filepath}: exit={returncode} "
                f"stderr={stderr.strip()!r}"
            )
        return stderr

    def _ensure_parquet(
        self,
        filepath: str,
        *,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        cancel_check: Optional[CancelCheck] = None,
    ) -> Path:
        """Return cached Parquet path, parsing QVD if cache is stale/missing.
        Only called from the scheduled/indexing warmup path — never from the hot
        query path.

        `progress_cb(done_rows, total_rows)` receives streamed conversion
        progress; `cancel_check` is polled so a long convert can be stopped.
        """
        file_hash, cache_path = self._cache_key(filepath)
        if cache_path.exists():
            return cache_path

        with _PARSE_LOCKS_MUTEX:
            lock = _PARSE_LOCKS.setdefault(cache_path, threading.Lock())

        with lock:
            # Re-check after acquiring; another thread may have finished the parse.
            if cache_path.exists():
                return cache_path

            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
            t_conv = time.perf_counter()
            logger.info(
                "qvd.convert.start",
                extra={
                    "qvd_file": filepath,
                    "qvd_tmp": str(tmp_path),
                    "qvd_bin": _QVD2PARQUET_BIN,
                },
            )
            # Streaming QVD → Parquet via standalone Rust binary. Bounded RAM
            # (symbol tables + ~64K-row chunk buffer) vs the in-process qvdrs
            # wheel which materialized the full table (~13× file size, OOM on 4GB+).
            stderr = self._run_convert_streaming(
                filepath, tmp_path, progress_cb=progress_cb, cancel_check=cancel_check,
            )

            try:
                parquet_bytes = tmp_path.stat().st_size
            except OSError:
                parquet_bytes = -1
            logger.info(
                "qvd.convert.done",
                extra={
                    "qvd_file": filepath,
                    "qvd_parquet_bytes": parquet_bytes,
                    "qvd_stderr": stderr.strip() or None,
                    "qvd_elapsed_s": round(time.perf_counter() - t_conv, 2),
                },
            )
            os.replace(tmp_path, cache_path)

        for old in _CACHE_DIR.glob(f"{file_hash}_*.parquet"):
            if old != cache_path:
                try:
                    old.unlink()
                except OSError:
                    pass
        return cache_path

    async def _warm_one(
        self,
        filepath: str,
        *,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        cancel_check: Optional[CancelCheck] = None,
    ) -> Path:
        """Parse one QVD to Parquet under the pod-wide warmup semaphore."""
        sem = _get_warmup_semaphore()
        async with sem:
            _, cache_path = self._cache_key(filepath)
            if cache_path.exists():
                logger.debug(
                    "qvd.warm.already_fresh",
                    extra={"qvd_file": filepath, "qvd_cache": str(cache_path)},
                )
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
                result = await asyncio.to_thread(
                    self._ensure_parquet, filepath,
                    progress_cb=progress_cb, cancel_check=cancel_check,
                )
            except IndexingCancelled:
                logger.info("qvd.warm.cancelled", extra={"qvd_file": filepath})
                raise
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
                "qvd.warm.done",
                extra={
                    "qvd_file": filepath,
                    "qvd_cache": str(result),
                    "qvd_elapsed_s": round(time.perf_counter() - t0, 2),
                },
            )
            return result

    async def aensure_warm(
        self,
        filepath: str,
        *,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        cancel_check: Optional[CancelCheck] = None,
    ) -> Path:
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
                task = asyncio.create_task(
                    self._warm_one(filepath, progress_cb=progress_cb, cancel_check=cancel_check)
                )
                _INFLIGHT[cache_path] = task

                def _cleanup(t: "asyncio.Task[Path]", key: Path = cache_path) -> None:
                    # Only drop the entry if it's still ours (mtime may have moved on).
                    if _INFLIGHT.get(key) is t:
                        _INFLIGHT.pop(key, None)

                task.add_done_callback(_cleanup)
            else:
                logger.debug(
                    "qvd.warm.dedup",
                    extra={"qvd_file": filepath, "qvd_cache": str(cache_path)},
                )
        return await task

    async def awarm_all(
        self,
        progress_callback: Optional[ProgressCallback] = None,
        cancel_check: Optional[CancelCheck] = None,
    ) -> List[Path]:
        """Warm every resolved QVD file. Errors on individual files are logged, not raised.

        When `progress_callback` is set, reports a "converting" phase whose
        done/total are *rows* across all files, so a single multi-GB file still
        moves the bar (the Rust converter streams per-chunk row counts). Honors
        `cancel_check` between files and mid-convert — a set cancel raises
        `IndexingCancelled` out of this call.
        """
        files = self._resolve_files()
        t0 = time.perf_counter()
        logger.info("qvd.warm_all.start", extra={"qvd_files": len(files), "qvd_patterns": self.patterns})

        # Pre-scan headers for a global row total so the bar reflects true work.
        per_file_rows = [self._read_qvd_record_count(f) for f in files]
        total_rows = sum(per_file_rows)
        reporter = make_reporter(progress_callback)
        # Fall back to file-count granularity when no header carried a row count.
        use_rows = total_rows > 0
        reporter.phase("converting", total=(total_rows if use_rows else len(files)))

        paths: List[Path] = []
        failed = 0
        base_done = 0  # rows (or files) completed before the current file
        for filepath, nrows in zip(files, per_file_rows):
            if cancel_check is not None and cancel_check():
                raise IndexingCancelled("QVD warm cancelled")
            basename = os.path.basename(filepath)

            def _file_progress(done_rows: int, file_total: int, _bn=basename) -> None:
                if use_rows:
                    reporter.item(_bn, done=min(total_rows, base_done + done_rows))

            try:
                paths.append(await self.aensure_warm(
                    filepath, progress_cb=_file_progress, cancel_check=cancel_check,
                ))
            except IndexingCancelled:
                raise
            except Exception:
                # _warm_one already logged qvd.warm.failed
                failed += 1
            base_done += nrows if use_rows else 1
            reporter.item(basename, done=base_done)
        reporter.done()
        logger.info(
            "qvd.warm_all.done",
            extra={
                "qvd_files": len(files),
                "qvd_warmed": len(paths),
                "qvd_failed": failed,
                "qvd_rows": total_rows,
                "qvd_elapsed_s": round(time.perf_counter() - t0, 2),
            },
        )
        return paths

    def index_stats(self) -> dict:
        """Source-size stats folded into the indexing row after warming. Sums
        the on-disk size and header row counts of the resolved QVD files."""
        files = self._resolve_files()
        total_bytes = 0
        for f in files:
            try:
                total_bytes += os.path.getsize(f)
            except OSError:
                pass
        return {
            "source_bytes": total_bytes,
            "file_count": len(files),
            "row_count": sum(self._read_qvd_record_count(f) for f in files),
        }

    @contextmanager
    def connect(self) -> Generator[duckdb.DuckDBPyConnection, None, None]:
        t0 = time.perf_counter()
        files = self._resolve_files()
        logger.info(
            "qvd.connect.start",
            extra={"qvd_patterns": self.patterns, "qvd_files_found": len(files)},
        )
        con: duckdb.DuckDBPyConnection | None = None
        try:
            con = duckdb.connect(database=":memory:")
            used: set[str] = set()
            table_map: dict[str, str] = {}
            skipped = 0
            for filepath in files:
                file_hash, fresh_path = self._cache_key(filepath)
                if fresh_path.exists():
                    parquet = fresh_path
                    logger.debug(
                        "qvd.connect.file.fresh",
                        extra={"qvd_file": filepath, "qvd_cache": str(parquet)},
                    )
                else:
                    parquet = self._find_any_parquet(file_hash)
                    if parquet is None:
                        logger.warning(
                            "qvd.connect.file.miss",
                            extra={
                                "qvd_file": filepath,
                                "qvd_hint": "no parquet cache found; run warmup job first",
                            },
                        )
                        skipped += 1
                        continue
                    logger.warning(
                        "qvd.connect.file.stale",
                        extra={
                            "qvd_file": filepath,
                            "qvd_cache": str(parquet),
                            "qvd_hint": "source file changed; serving stale cache until warmup completes",
                        },
                    )

                table_name = self._safe_table_name(filepath, used)
                parquet_sql = str(parquet).replace("'", "''")
                con.execute(
                    f"CREATE VIEW {table_name} AS SELECT * FROM read_parquet('{parquet_sql}')"
                )
                table_map[table_name] = filepath

            self._table_map = table_map
            logger.info(
                "qvd.connect.done",
                extra={
                    "qvd_tables": list(table_map.keys()),
                    "qvd_skipped": skipped,
                    "qvd_elapsed_s": round(time.perf_counter() - t0, 3),
                },
            )
            yield con
        except Exception as e:
            logger.error(
                "qvd.connect.error",
                extra={"qvd_error": str(e), "qvd_elapsed_s": round(time.perf_counter() - t0, 3)},
            )
            raise RuntimeError(f"Error connecting to QVD files: {e}")
        finally:
            if con is not None:
                con.close()

    def execute_query(self, sql: str) -> pd.DataFrame:
        t0 = time.perf_counter()
        logger.info("qvd.query.start", extra={"qvd_sql": sql})
        try:
            with self.connect() as con:
                df = con.execute(sql).df()
            logger.info(
                "qvd.query.done",
                extra={
                    "qvd_sql": sql,
                    "qvd_rows": len(df),
                    "qvd_cols": len(df.columns),
                    "qvd_elapsed_s": round(time.perf_counter() - t0, 3),
                },
            )
            return df
        except Exception as exc:
            logger.error(
                "qvd.query.error",
                extra={
                    "qvd_sql": sql,
                    "qvd_error": str(exc),
                    "qvd_elapsed_s": round(time.perf_counter() - t0, 3),
                },
            )
            raise

    def execute_query_lazy(self, sql: str):
        """Out-of-core variant (v2): COPY results straight to Parquet via DuckDB
        (zero Python materialization), return a LazyFrame over it."""
        from app.data_sources.clients.lazy_frame import lazy_query_via_duckdb
        return lazy_query_via_duckdb(self.connect, sql)

    def get_tables(self, progress_callback: Optional[ProgressCallback] = None) -> List[Table]:
        """
        Schema lookup. Uses DuckDB DESCRIBE on the cached Parquet when available
        (ground truth for queries); otherwise falls back to the QVD XML header.
        Both paths return DuckDB-compatible type names.
        """
        tables: List[Table] = []
        used: set[str] = set()
        files = self._resolve_files()
        logger.debug("qvd.schema.start", extra={"qvd_files": len(files)})
        reporter = make_reporter(progress_callback)
        reporter.phase("qvd_files", total=len(files))
        for filepath in files:
            reporter.item(os.path.basename(filepath))
            table_name = self._safe_table_name(filepath, used)
            cols: List[TableColumn] = []
            try:
                file_hash, cache_path = self._cache_key(filepath)
                if cache_path.exists():
                    fields = self._describe_parquet(cache_path)
                    logger.debug(
                        "qvd.schema.file.parquet",
                        extra={"qvd_file": filepath, "qvd_fields": len(fields)},
                    )
                else:
                    stale = self._find_any_parquet(file_hash)
                    if stale:
                        fields = self._describe_parquet(stale)
                        logger.debug(
                            "qvd.schema.file.stale_parquet",
                            extra={"qvd_file": filepath, "qvd_cache": str(stale), "qvd_fields": len(fields)},
                        )
                    else:
                        fields = self._read_qvd_header(filepath)
                        logger.debug(
                            "qvd.schema.file.header",
                            extra={"qvd_file": filepath, "qvd_fields": len(fields)},
                        )
                for fname, ftype in fields:
                    cols.append(TableColumn(name=fname, dtype=ftype))
            except Exception as exc:
                logger.warning(
                    "qvd.schema.file.error",
                    extra={"qvd_file": filepath, "qvd_error": str(exc)},
                )
            tables.append(Table(
                name=table_name,
                columns=cols,
                pks=[],
                fks=[],
                metadata_json={"qvd": {"source_file": filepath}}
            ))
        reporter.done()
        logger.debug("qvd.schema.done", extra={"qvd_tables": len(tables)})
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
        """Lightweight check: verifies files exist and headers are valid. No data load."""
        try:
            files = self._resolve_files()
            if not files:
                return {
                    "success": False,
                    "message": "No QVD files found matching the patterns",
                    "details": {"files_found": 0, "patterns": self.patterns},
                }
            total_bytes = 0
            cached_parquets = 0
            for f in files:
                try:
                    total_bytes += os.path.getsize(f)
                except OSError:
                    pass
                _, cache_path = self._cache_key(f)
                if cache_path.exists():
                    cached_parquets += 1
                self._read_qvd_header(f)
            return {
                "success": True,
                "message": f"Successfully verified {len(files)} QVD file(s)",
                "details": {
                    "files_found": len(files),
                    "total_bytes": total_bytes,
                    "cached_parquets": cached_parquets,
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
    import asyncio
    from app.core.scheduler import claim_scheduled_run
    # Every worker runs a scheduler against the shared job store, so this can
    # fire in multiple workers at once. Claim the fire so only one warms.
    if not await asyncio.to_thread(claim_scheduled_run, "qvd_warmup"):
        return

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

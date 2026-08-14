from __future__ import annotations

import asyncio
import contextlib
import glob
import hashlib
import json
import os
import time
import zipfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pandas as pd

from app.ai.prompt_formatters import ForeignKey, Table, TableColumn, TableFormatter
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.pbix_common import (
    PBIX_MAX_BYTES,
    extract_pbix_model,
    materialize_pbix_parquets,
    safe_view_name,
)
from app.data_sources.clients.progress import (
    CancelCheck,
    IndexingCancelled,
    ProgressCallback,
    make_reporter,
)
from app.settings.logging_config import get_logger

logger = get_logger(__name__)

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "uploads" / "pbix_file_cache"
# Bumped when the extraction pipeline changes what it writes, so old caches are
# reconverted instead of served stale.
_CACHE_SCHEMA_VERSION = 1

# One in-process parse at a time per pod: pbixray decodes tables fully in
# memory, so parallel parses of large models would multiply peak RAM.
_WARMUP_SEMAPHORE: asyncio.Semaphore | None = None


def _get_warmup_semaphore() -> asyncio.Semaphore:
    global _WARMUP_SEMAPHORE
    if _WARMUP_SEMAPHORE is None:
        _WARMUP_SEMAPHORE = asyncio.Semaphore(1)
    return _WARMUP_SEMAPHORE


class PBIXClient(DataSourceClient):
    """Read Power BI (.pbix) files from disk and query their embedded semantic
    models via SQL using DuckDB.

    Each .pbix contains a Vertipaq (import-mode) model with one or more tables.
    Tables are decoded with pbixray, materialized to a Parquet cache keyed by
    (file path, mtime), and registered as DuckDB views so they can be queried
    and JOINed with plain SQL. Data reflects the last refresh baked into the
    file — editing/refreshing the .pbix invalidates the cache automatically.
    """

    def __init__(self, file_paths: str):
        """
        Args:
            file_paths: Newline-separated list of file paths or glob patterns.
                        e.g., "/data/*.pbix" or "/data/Sales.pbix\n/data/HR.pbix"
        """
        self.file_paths_raw = file_paths or ""
        self.patterns: list[str] = [
            p.strip() for p in self.file_paths_raw.splitlines() if p.strip()
        ]

    # ------------------------------------------------------------------
    # File resolution / cache plumbing
    # ------------------------------------------------------------------

    def _resolve_files(self) -> list[str]:
        """Expand glob patterns to actual .pbix file paths."""
        files: list[str] = []
        for pattern in self.patterns:
            matched = glob.glob(pattern, recursive=True)
            files.extend([f for f in matched if f.lower().endswith(".pbix")])
        return sorted(set(files))

    @staticmethod
    def _cache_key(filepath: str) -> tuple[str, Path]:
        """Return (file_hash, cache_dir) for the given file + its current mtime."""
        abs_path = os.path.abspath(filepath)
        file_hash = hashlib.sha256(abs_path.encode("utf-8")).hexdigest()[:16]
        mtime_ns = os.stat(abs_path).st_mtime_ns
        return file_hash, _CACHE_DIR / f"{file_hash}_{mtime_ns}_v{_CACHE_SCHEMA_VERSION}"

    @staticmethod
    def _find_any_cache(file_hash: str) -> Path | None:
        """Return the newest complete cache dir for this file hash, any mtime,
        so a just-edited file still serves (stale) data until re-warmed."""
        candidates = sorted(
            (
                p for p in _CACHE_DIR.glob(f"{file_hash}_*_v{_CACHE_SCHEMA_VERSION}")
                if (p / "manifest.json").exists()
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return candidates[0] if candidates else None

    @staticmethod
    def _read_manifest(cache_dir: Path) -> dict[str, str] | None:
        manifest_path = cache_dir / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            with manifest_path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.debug(f"pbix manifest read failed for {cache_dir}: {e}")
            return None

    def _load_model_schema(self, filepath: str, cache_dir: Path) -> dict | None:
        """Model schema (tables/columns/measures/relationships) for one file.
        Served from schema.json in the cache dir when present; otherwise parsed
        with pbixray (metadata only, no data decode) and cached there."""
        schema_path = cache_dir / "schema.json"
        if schema_path.exists():
            try:
                with schema_path.open("r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.debug(f"pbix schema cache read failed for {filepath}: {e}")
        try:
            model = extract_pbix_model(filepath)
        except Exception as e:
            logger.warning(
                "pbix.schema.file.error",
                extra={"pbix_file": filepath, "pbix_error": str(e)},
            )
            return None
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            with schema_path.open("w", encoding="utf-8") as f:
                json.dump(model, f)
        except Exception as e:
            logger.debug(f"pbix schema cache write failed for {filepath}: {e}")
        return model

    def _ensure_parquets(self, filepath: str) -> tuple[Path, dict[str, str]]:
        """Return (cache_dir, manifest) for a file, materializing the Parquet
        cache if it's missing. Called from the warm path and — because the
        source files are local and decode is the same cost either way — from
        the query path on a cold cache."""
        file_hash, cache_dir = self._cache_key(filepath)
        manifest = self._read_manifest(cache_dir)
        if manifest is not None:
            return cache_dir, manifest

        size = os.path.getsize(filepath)
        if size > PBIX_MAX_BYTES:
            raise RuntimeError(
                f"PBIX file '{filepath}' is {size} bytes (> {PBIX_MAX_BYTES} limit); "
                "refusing to materialize."
            )
        t0 = time.perf_counter()
        logger.info("pbix.convert.start", extra={"pbix_file": filepath, "pbix_bytes": size})
        manifest = materialize_pbix_parquets(filepath, cache_dir)
        logger.info(
            "pbix.convert.done",
            extra={
                "pbix_file": filepath,
                "pbix_tables": len(manifest),
                "pbix_elapsed_s": round(time.perf_counter() - t0, 2),
            },
        )
        return cache_dir, manifest

    # ------------------------------------------------------------------
    # Table naming
    # ------------------------------------------------------------------

    def _view_names(self, per_file_tables: dict[str, list[str]]) -> dict[tuple[str, str], str]:
        """Assign a DuckDB view name to every (filepath, model_table).

        Model table names are used directly (sanitized) when unique across all
        configured files; a table name appearing in several files gets prefixed
        with the file's basename. Residual collisions get numeric suffixes.
        """
        counts: dict[str, int] = {}
        for tnames in per_file_tables.values():
            for tname in tnames:
                key = safe_view_name(tname).lower()
                counts[key] = counts.get(key, 0) + 1

        out: dict[tuple[str, str], str] = {}
        used: set = set()
        for filepath in sorted(per_file_tables):
            base = safe_view_name(os.path.splitext(os.path.basename(filepath))[0]).lower()
            for tname in per_file_tables[filepath]:
                safe = safe_view_name(tname)
                view = safe if counts[safe.lower()] == 1 else f"{base}_{safe}"
                original = view
                i = 1
                while view.lower() in used:
                    i += 1
                    view = f"{original}_{i}"
                used.add(view.lower())
                out[(filepath, tname)] = view
        return out

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    @contextmanager
    def connect(self) -> Generator[duckdb.DuckDBPyConnection, None, None]:
        t0 = time.perf_counter()
        files = self._resolve_files()
        logger.info(
            "pbix.connect.start",
            extra={"pbix_patterns": self.patterns, "pbix_files_found": len(files)},
        )
        con: duckdb.DuckDBPyConnection | None = None
        try:
            con = duckdb.connect(database=":memory:")
            per_file_manifest: dict[str, tuple[Path, dict[str, str]]] = {}
            skipped = 0
            for filepath in files:
                file_hash, fresh_dir = self._cache_key(filepath)
                manifest = self._read_manifest(fresh_dir)
                if manifest is not None:
                    per_file_manifest[filepath] = (fresh_dir, manifest)
                    continue
                stale_dir = self._find_any_cache(file_hash)
                if stale_dir is not None:
                    logger.warning(
                        "pbix.connect.file.stale",
                        extra={
                            "pbix_file": filepath,
                            "pbix_cache": str(stale_dir),
                            "pbix_hint": "source file changed; serving stale cache until warmup completes",
                        },
                    )
                    per_file_manifest[filepath] = (stale_dir, self._read_manifest(stale_dir) or {})
                    continue
                try:
                    per_file_manifest[filepath] = self._ensure_parquets(filepath)
                except Exception as e:
                    logger.warning(
                        "pbix.connect.file.error",
                        extra={"pbix_file": filepath, "pbix_error": str(e)},
                    )
                    skipped += 1

            views = self._view_names(
                {f: sorted(m.keys()) for f, (_, m) in per_file_manifest.items()}
            )
            for filepath, (cache_dir, manifest) in per_file_manifest.items():
                for tname, fname in manifest.items():
                    ppath = cache_dir / fname
                    if not ppath.exists():
                        continue
                    view = views[(filepath, tname)]
                    parquet_sql = str(ppath).replace("'", "''")
                    con.execute(
                        f'CREATE VIEW "{view}" AS SELECT * FROM read_parquet(\'{parquet_sql}\')'
                    )

            logger.info(
                "pbix.connect.done",
                extra={
                    "pbix_views": sorted(v for v in views.values()),
                    "pbix_skipped": skipped,
                    "pbix_elapsed_s": round(time.perf_counter() - t0, 3),
                },
            )
            yield con
        except Exception as e:
            logger.error(
                "pbix.connect.error",
                extra={"pbix_error": str(e), "pbix_elapsed_s": round(time.perf_counter() - t0, 3)},
            )
            raise RuntimeError(f"Error connecting to PBIX files: {e}")
        finally:
            if con is not None:
                con.close()

    def execute_query(self, sql: str) -> pd.DataFrame:
        t0 = time.perf_counter()
        logger.info("pbix.query.start", extra={"pbix_sql": sql})
        try:
            with self.connect() as con:
                df = con.execute(sql).df()
            logger.info(
                "pbix.query.done",
                extra={
                    "pbix_sql": sql,
                    "pbix_rows": len(df),
                    "pbix_cols": len(df.columns),
                    "pbix_elapsed_s": round(time.perf_counter() - t0, 3),
                },
            )
            return df
        except Exception as exc:
            logger.error(
                "pbix.query.error",
                extra={
                    "pbix_sql": sql,
                    "pbix_error": str(exc),
                    "pbix_elapsed_s": round(time.perf_counter() - t0, 3),
                },
            )
            raise

    # ------------------------------------------------------------------
    # Schema discovery
    # ------------------------------------------------------------------

    def get_tables(self, progress_callback: ProgressCallback | None = None) -> list[Table]:
        files = self._resolve_files()
        reporter = make_reporter(progress_callback)
        reporter.phase("pbix_files", total=len(files))
        logger.debug("pbix.schema.start", extra={"pbix_files": len(files)})

        # First pass: per-file model schemas (cheap — metadata only).
        per_file_model: dict[str, dict] = {}
        for filepath in files:
            reporter.item(os.path.basename(filepath))
            _, cache_dir = self._cache_key(filepath)
            model = self._load_model_schema(filepath, cache_dir)
            if model is not None:
                per_file_model[filepath] = model

        views = self._view_names(
            {
                f: [t["name"] for t in (m.get("tables") or [])]
                for f, m in per_file_model.items()
            }
        )

        tables: list[Table] = []
        for filepath, model in per_file_model.items():
            file_hash, cache_dir = self._cache_key(filepath)
            manifest = self._read_manifest(cache_dir)
            if manifest is None:
                stale_dir = self._find_any_cache(file_hash)
                if stale_dir is not None:
                    manifest = self._read_manifest(stale_dir)
                    cache_dir = stale_dir

            rels = model.get("relationships") or []
            rels_by_from: dict[str, list[dict]] = {}
            for rel in rels:
                rels_by_from.setdefault(rel["from_table"], []).append(rel)

            for st in model.get("tables") or []:
                tname = st["name"]
                view = views[(filepath, tname)]
                columns = [
                    TableColumn(name=c["name"], dtype=c.get("dtype") or "unknown")
                    for c in st.get("columns", [])
                ]
                col_by_name = {c.name: c for c in columns}

                fks: list[ForeignKey] = []
                for rel in rels_by_from.get(tname, []):
                    fc = col_by_name.get(rel["from_column"])
                    ref_view = views.get((filepath, rel["to_table"]))
                    if fc is None or ref_view is None:
                        continue
                    fks.append(ForeignKey(
                        column=fc,
                        references_name=ref_view,
                        references_column=TableColumn(name=rel["to_column"], dtype="unknown"),
                    ))

                materialized = bool(manifest and tname in manifest)
                tables.append(Table(
                    name=view,
                    description=(
                        f"Table '{tname}' from Power BI file "
                        f"'{os.path.basename(filepath)}'. Query with DuckDB SQL."
                    ),
                    columns=columns,
                    pks=[],
                    fks=fks,
                    is_active=True,
                    metadata_json={
                        "pbix": {
                            "source_file": filepath,
                            "model_table": tname,
                            "measures": st.get("measures", []),
                            "materialized": materialized,
                        }
                    },
                ))

        reporter.done()
        logger.debug("pbix.schema.done", extra={"pbix_tables": len(tables)})
        return tables

    def get_schemas(self, progress_callback: ProgressCallback | None = None) -> list[Table]:
        return self.get_tables(progress_callback=progress_callback)

    def get_schema(self, table_name: str) -> Table:
        for t in self.get_tables():
            if t.name == table_name:
                return t
        return Table(name=table_name, columns=[], pks=[], fks=[], metadata_json={})

    def prompt_schema(self) -> str:
        return TableFormatter(self.get_schemas()).table_str

    # ------------------------------------------------------------------
    # Warmup / indexing integration
    # ------------------------------------------------------------------

    async def awarm_all(
        self,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> list[Path]:
        """Materialize the Parquet cache for every resolved file. Individual
        file failures are logged, not raised; honors `cancel_check` between
        files. Runs under a pod-wide semaphore since pbixray decodes in-process."""
        files = self._resolve_files()
        t0 = time.perf_counter()
        logger.info("pbix.warm_all.start", extra={"pbix_files": len(files), "pbix_patterns": self.patterns})
        reporter = make_reporter(progress_callback)
        reporter.phase("converting", total=len(files))

        sem = _get_warmup_semaphore()
        paths: list[Path] = []
        failed = 0
        for done, filepath in enumerate(files, start=1):
            if cancel_check is not None and cancel_check():
                raise IndexingCancelled("PBIX warm cancelled")
            async with sem:
                try:
                    cache_dir, _ = await asyncio.to_thread(self._ensure_parquets, filepath)
                    paths.append(cache_dir)
                except Exception as exc:
                    logger.warning(
                        "pbix.warm.failed",
                        extra={"pbix_file": filepath, "pbix_error": str(exc)},
                    )
                    failed += 1
            reporter.item(os.path.basename(filepath), done=done)
        reporter.done()
        logger.info(
            "pbix.warm_all.done",
            extra={
                "pbix_files": len(files),
                "pbix_warmed": len(paths),
                "pbix_failed": failed,
                "pbix_elapsed_s": round(time.perf_counter() - t0, 2),
            },
        )
        return paths

    def index_stats(self) -> dict:
        files = self._resolve_files()
        total_bytes = 0
        for f in files:
            with contextlib.suppress(OSError):
                total_bytes += os.path.getsize(f)
        return {"source_bytes": total_bytes, "file_count": len(files)}

    # ------------------------------------------------------------------
    # Connection test
    # ------------------------------------------------------------------

    def test_connection(self) -> dict:
        """Lightweight check: files exist, are ZIP containers, and carry an
        embedded (import-mode) data model. No table data is decoded."""
        try:
            files = self._resolve_files()
            if not files:
                return {
                    "success": False,
                    "message": "No .pbix files found matching the patterns",
                    "details": {"files_found": 0, "patterns": self.patterns},
                }
            total_bytes = 0
            no_model: list[str] = []
            cached = 0
            for f in files:
                with contextlib.suppress(OSError):
                    total_bytes += os.path.getsize(f)
                try:
                    with zipfile.ZipFile(f) as zf:
                        names = zf.namelist()
                    if not any(n == "DataModel" or n.endswith("/DataModel") for n in names):
                        no_model.append(os.path.basename(f))
                except zipfile.BadZipFile:
                    return {
                        "success": False,
                        "message": f"Not a valid .pbix (not a ZIP container): {f}",
                        "details": {"files_found": len(files)},
                    }
                file_hash, cache_dir = self._cache_key(f)
                if (cache_dir / "manifest.json").exists():
                    cached += 1

            message = f"Successfully verified {len(files)} PBIX file(s)"
            if no_model:
                message += (
                    f". Warning: {len(no_model)} file(s) have no embedded data model "
                    f"(live/DirectQuery reports are not queryable): {', '.join(no_model[:3])}"
                )
            return {
                "success": True,
                "message": message,
                "details": {
                    "files_found": len(files),
                    "total_bytes": total_bytes,
                    "cached": cached,
                    "no_embedded_model": no_model,
                    "sample_files": [os.path.basename(f) for f in files[:5]],
                },
            }
        except Exception as e:
            return {"success": False, "message": str(e), "details": {}}

    # ------------------------------------------------------------------
    # Prompt / description
    # ------------------------------------------------------------------

    @property
    def description(self) -> str:
        files = self._resolve_files()
        sample = ", ".join([os.path.basename(f) for f in files[:3]])
        if len(files) > 3:
            sample += ", ..."

        return f"""Power BI (.pbix) files: {sample}

Each file's embedded semantic model is exposed as queryable tables (DuckDB SQL).
Tables from all configured files share one session, so they can be JOINed freely
using the table names shown in the schema. Data reflects the last refresh saved
into each .pbix file, not a live source.

DAX measures (in each table's `metadata_json.pbix.measures`) are expressions,
not values — rewrite them as SQL aggregates over the exported columns.

Examples:
```python
df = client.execute_query("SELECT * FROM Sales LIMIT 10")
```
```python
df = client.execute_query(
    "SELECT s.Category, SUM(o.Amount) AS total FROM Orders o "
    "JOIN Sales s ON o.SaleID = s.ID GROUP BY s.Category"
)
```
"""


PbixClient = PBIXClient


async def warm_all_pbix_file_caches() -> None:
    """Scheduled maintenance: walk every active PBIX Connection and ensure its
    Parquet caches are warm, so the first query after a file edit doesn't pay
    the in-process pbixray decode. Designed for an APScheduler interval."""
    from app.core.scheduler import claim_scheduled_run
    # Fires in every worker (shared job store); claim so only one warms.
    if not await asyncio.to_thread(claim_scheduled_run, "pbix_file_warmup"):
        return

    from sqlalchemy import select

    from app.dependencies import async_session_maker
    from app.models.connection import Connection

    t0 = time.perf_counter()
    async with async_session_maker() as db:
        rows = (
            await db.execute(
                select(Connection).where(
                    Connection.type == "pbix",
                    Connection.is_active.is_(True),
                    Connection.deleted_at.is_(None),
                )
            )
        ).scalars().all()

    if not rows:
        logger.info("pbix.warmup.sweep", extra={"pbix_connections": 0})
        return

    logger.info("pbix.warmup.sweep.start", extra={"pbix_connections": len(rows)})
    warmed = 0
    failed = 0
    for conn in rows:
        try:
            client = conn.get_client()
        except Exception as exc:
            logger.warning(
                "pbix.warmup.client_init_failed",
                extra={"connection_id": str(conn.id), "pbix_error": str(exc)},
            )
            failed += 1
            continue
        if not isinstance(client, PBIXClient):
            continue
        try:
            await client.awarm_all()
            warmed += 1
        except Exception as exc:
            logger.warning(
                "pbix.warmup.connection_failed",
                extra={"connection_id": str(conn.id), "pbix_error": str(exc)},
            )
            failed += 1

    logger.info(
        "pbix.warmup.sweep.done",
        extra={
            "pbix_connections": len(rows),
            "pbix_warmed": warmed,
            "pbix_failed": failed,
            "pbix_elapsed_s": round(time.perf_counter() - t0, 2),
        },
    )

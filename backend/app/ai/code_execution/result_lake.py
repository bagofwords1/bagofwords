"""Result Lake — a process-local query result cache.

Goal: stop re-querying upstream data sources for queries the AI agent has
already run, and read reused results back from disk (Parquet) instead of holding
them in memory forever.

Design principles (see docs/design/query-result-cache.md):
  * A cache MISS is always correct — when in doubt we simply don't serve from
    cache and let the caller hit the source. The cache can never return wrong
    data, only stale-within-TTL data.
  * Defaults OFF. Enabled via BOW_RESULT_CACHE_ENABLED=1. A cache failure must
    never break query execution — every public method swallows its own errors
    and degrades to "miss".
  * Storage substrate is Parquet on local disk; reads go through pandas/pyarrow
    (already dependencies). DuckDB/chDB/ClickHouse engines are a future swap.

Scope of this module: Phase 1 — TTL exact-match cache with cost-aware eviction.
Subsumption, version-token invalidation and incremental refresh are documented
as opt-in upgrades and intentionally NOT implemented here.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)


def _normalize_sql(sql: str) -> str:
    """Canonicalize a SQL string for keying.

    Prefer sqlglot (collapses whitespace/casing/alias noise across dialects) but
    fall back to a cheap whitespace+case normalization if it is unavailable or
    cannot parse. The fallback is always safe: a worse normalization only lowers
    the hit rate, it never produces a wrong hit (different SQL → different key).
    """
    try:
        import sqlglot  # optional dependency

        return sqlglot.transpile(sql, identify=True, pretty=False)[0]
    except Exception:
        return " ".join(sql.split()).lower()


def _default_cache_root() -> Path:
    return Path(tempfile.gettempdir()) / "bow_result_cache"


def _link_or_copy(src: Path, dst: Path) -> None:
    """Hardlink src->dst when on the same filesystem (cheap, shares the inode so
    the data survives until both names are unlinked); fall back to a copy across
    devices."""
    try:
        os.link(src, dst)
    except OSError:
        import shutil

        shutil.copyfile(src, dst)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


@dataclass
class CacheConfig:
    enabled: bool = False
    root: Path = field(default_factory=_default_cache_root)
    max_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GiB on-disk budget
    ttl_seconds: float = 300.0
    min_cost_ms: float = 250.0  # don't cache queries cheaper than this to run

    # Serve a narrower single-table query from a cached full-table scan (off by
    # default — it is the most aggressive optimization; see subsumption.py).
    subsumption_enabled: bool = False

    # Source classes whose data moves fast enough to warrant a tighter TTL.
    # Matched as a case-insensitive substring against the client class name.
    fast_source_ttl_seconds: float = 30.0
    fast_source_markers: tuple[str, ...] = ("druid", "pinot")

    @classmethod
    def from_env(cls) -> "CacheConfig":
        root_env = os.environ.get("BOW_RESULT_CACHE_DIR")
        return cls(
            enabled=os.environ.get("BOW_RESULT_CACHE_ENABLED", "0").lower() in ("1", "true", "yes"),
            root=Path(root_env) if root_env else _default_cache_root(),
            max_bytes=_env_int("BOW_RESULT_CACHE_MAX_BYTES", 2 * 1024 * 1024 * 1024),
            ttl_seconds=_env_float("BOW_RESULT_CACHE_TTL_SECONDS", 300.0),
            min_cost_ms=_env_float("BOW_RESULT_CACHE_MIN_COST_MS", 250.0),
            subsumption_enabled=os.environ.get("BOW_RESULT_CACHE_SUBSUMPTION", "0").lower()
            in ("1", "true", "yes"),
        )

    def ttl_for(self, source_class: str) -> float:
        sc = (source_class or "").lower()
        if any(m in sc for m in self.fast_source_markers):
            return min(self.ttl_seconds, self.fast_source_ttl_seconds)
        return self.ttl_seconds


@dataclass
class CacheEntry:
    key: str
    path: Path
    size_bytes: int
    cost_ms: float
    source_class: str
    created_at: float
    hits: int = 0
    scope: str = ""
    shape: object = None  # subsumption.QueryShape | None

    def value(self) -> float:
        """Eviction priority: keep entries that were expensive to produce and
        are reused, relative to the space they cost. Lowest value is evicted."""
        return (self.cost_ms * (self.hits + 1)) / max(self.size_bytes, 1)


class ResultLake:
    """Thread-safe, process-local Parquet result cache.

    The index lives in memory; on a cold start the on-disk root is swept so we
    never inherit untracked Parquet files from a previous process.
    """

    def __init__(self, config: CacheConfig):
        self.config = config
        self._lock = threading.RLock()
        self._index: Dict[str, CacheEntry] = {}
        self._total_bytes = 0
        if self.config.enabled:
            try:
                self.config.root.mkdir(parents=True, exist_ok=True)
                self._sweep_orphans()
            except Exception:  # pragma: no cover - never block startup
                logger.exception("ResultLake: failed to initialize cache root; disabling")
                self.config.enabled = False

    # -- public API ---------------------------------------------------------

    def get(self, scope: str, sql: str, source_class: str = "") -> Optional[pd.DataFrame]:
        """Return a cached DataFrame for (scope, sql) if fresh, else None."""
        if not self.config.enabled or not isinstance(sql, str):
            return None
        try:
            key = self._key(scope, sql)
            with self._lock:
                entry = self._index.get(key)
                if entry is None:
                    return None
                age = time.monotonic() - entry.created_at
                if age > self.config.ttl_for(entry.source_class):
                    self._remove_locked(key)
                    return None
                path = entry.path
            df = pd.read_parquet(path)  # read outside the lock
            with self._lock:
                # entry may have been evicted concurrently; bumping a stale entry
                # is harmless since we already hold the DataFrame.
                if key in self._index:
                    self._index[key].hits += 1
            return df
        except Exception:
            logger.debug("ResultLake.get failed; treating as miss", exc_info=True)
            return None

    def put(self, scope: str, sql: str, df: object, cost_ms: float, source_class: str = "") -> None:
        """Cache a result DataFrame. No-ops on anything we can't/shouldn't store."""
        if not self.config.enabled or not isinstance(sql, str):
            return
        if not isinstance(df, pd.DataFrame):
            return
        if cost_ms < self.config.min_cost_ms:
            return  # too cheap to be worth caching
        try:
            key = self._key(scope, sql)
            path = self.config.root / f"{key}.parquet"
            tmp = self.config.root / f".{key}.{uuid.uuid4().hex}.tmp"
            df.to_parquet(tmp, index=False)
            os.replace(tmp, path)  # atomic publish
            size = path.stat().st_size
            with self._lock:
                if key in self._index:
                    self._remove_locked(key)  # replace older copy, fix byte accounting
                self._index[key] = CacheEntry(
                    key=key,
                    path=path,
                    size_bytes=size,
                    cost_ms=float(cost_ms),
                    source_class=source_class or "",
                    created_at=time.monotonic(),
                    scope=scope,
                    shape=self._shape_for(sql),
                )
                self._total_bytes += size
                self._evict_to_fit_locked()
        except Exception:
            logger.debug("ResultLake.put failed; result not cached", exc_info=True)

    def get_owned_copy(self, scope: str, sql: str, source_class: str, dest_dir) -> Optional[Path]:
        """Cache lookup for the lazy path. On a fresh hit, hardlink/copy the cached
        Parquet into `dest_dir` under a unique name and return that path — the caller
        owns it (deletes on close), so cache eviction can't pull the file out from
        under an in-flight LazyFrame. Returns None on miss/expired/disabled."""
        if not self.config.enabled or not isinstance(sql, str):
            return None
        try:
            key = self._key(scope, sql)
            with self._lock:
                entry = self._index.get(key)
                if entry is None:
                    return None
                if time.monotonic() - entry.created_at > self.config.ttl_for(entry.source_class):
                    self._remove_locked(key)
                    return None
                src = entry.path
            dest = Path(dest_dir)
            dest.mkdir(parents=True, exist_ok=True)
            out = dest / f"hit_{uuid.uuid4().hex}.parquet"
            _link_or_copy(src, out)
            with self._lock:
                if key in self._index:
                    self._index[key].hits += 1
            return out
        except Exception:
            logger.debug("ResultLake.get_owned_copy failed; treating as miss", exc_info=True)
            return None

    def register_path(self, scope: str, sql: str, src_path, cost_ms: float, source_class: str = "") -> None:
        """Cache insert for the lazy path: adopt an already-written Parquet file
        (the streamed result) by hardlinking/copying it to the canonical cache path.
        Shares the index/key/eviction with the DataFrame path, so lazy and non-lazy
        results for the same query interoperate."""
        if not self.config.enabled or not isinstance(sql, str):
            return
        if cost_ms < self.config.min_cost_ms:
            return
        try:
            key = self._key(scope, sql)
            with self._lock:
                if key in self._index:
                    return  # already cached & fresh; don't duplicate work
            dest = self.config.root / f"{key}.parquet"
            tmp = self.config.root / f".{key}.{uuid.uuid4().hex}.tmp"
            _link_or_copy(Path(src_path), tmp)
            os.replace(tmp, dest)  # atomic publish
            size = dest.stat().st_size
            with self._lock:
                if key in self._index:
                    self._remove_locked(key)
                self._index[key] = CacheEntry(
                    key=key,
                    path=dest,
                    size_bytes=size,
                    cost_ms=float(cost_ms),
                    source_class=source_class or "",
                    created_at=time.monotonic(),
                    scope=scope,
                    shape=self._shape_for(sql),
                )
                self._total_bytes += size
                self._evict_to_fit_locked()
        except Exception:
            logger.debug("ResultLake.register_path failed; result not cached", exc_info=True)

    # -- subsumption: serve a narrower query from a cached full table scan --

    def _shape_for(self, sql: str):
        if not self.config.subsumption_enabled:
            return None
        try:
            from app.ai.code_execution.subsumption import analyze
            return analyze(sql)
        except Exception:
            return None

    def _find_subsumption(self, scope: str, sql: str):
        """Return (entry, rewritten_duckdb_sql) for a cached full-scan that can
        answer `sql`, preferring the smallest candidate; else None."""
        if not self.config.subsumption_enabled:
            return None
        try:
            from app.ai.code_execution.subsumption import analyze, can_subsume, rewrite_onto_parquet
        except Exception:
            return None
        new_shape = analyze(sql)
        if not (new_shape.analyzable and new_shape.single_table):
            return None
        now = time.monotonic()
        with self._lock:
            candidates = [
                e for e in self._index.values()
                if e.scope == scope and e.shape is not None
                and getattr(e.shape, "is_full_scan", False)
                and (now - e.created_at) <= self.config.ttl_for(e.source_class)
            ]
            candidates.sort(key=lambda e: e.size_bytes)
        for entry in candidates:
            if can_subsume(entry.shape, new_shape):
                rewritten = rewrite_onto_parquet(sql, str(entry.path))
                if rewritten:
                    return entry, rewritten
        return None

    def get_subsuming_df(self, scope: str, sql: str, source_class: str = ""):
        """DataFrame path: compute `sql` from a subsuming cached full-scan, or None."""
        if not self.config.enabled or not self.config.subsumption_enabled:
            return None
        try:
            found = self._find_subsumption(scope, sql)
            if found is None:
                return None
            entry, rewritten = found
            import duckdb
            con = duckdb.connect(database=":memory:")
            try:
                df = con.execute(rewritten).df()
            finally:
                con.close()
            with self._lock:
                if entry.key in self._index:
                    self._index[entry.key].hits += 1
            return df
        except Exception:
            logger.debug("ResultLake.get_subsuming_df failed; treating as miss", exc_info=True)
            return None

    def get_subsuming_path(self, scope: str, sql: str, source_class: str, dest_dir) -> Optional[Path]:
        """Lazy path: compute `sql` from a subsuming cached full-scan into a new
        owned Parquet in `dest_dir` (via DuckDB COPY), or None."""
        if not self.config.enabled or not self.config.subsumption_enabled:
            return None
        try:
            found = self._find_subsumption(scope, sql)
            if found is None:
                return None
            entry, rewritten = found
            dest = Path(dest_dir)
            dest.mkdir(parents=True, exist_ok=True)
            out = dest / f"sub_{uuid.uuid4().hex}.parquet"
            target = str(out).replace("'", "''")
            import duckdb
            con = duckdb.connect(database=":memory:")
            try:
                con.execute(f"COPY ({rewritten}) TO '{target}' (FORMAT PARQUET)")
            finally:
                con.close()
            with self._lock:
                if entry.key in self._index:
                    self._index[entry.key].hits += 1
            return out
        except Exception:
            logger.debug("ResultLake.get_subsuming_path failed; treating as miss", exc_info=True)
            return None

    def stats(self) -> dict:
        with self._lock:
            return {
                "enabled": self.config.enabled,
                "entries": len(self._index),
                "total_bytes": self._total_bytes,
                "max_bytes": self.config.max_bytes,
            }

    # -- internals ----------------------------------------------------------

    def _key(self, scope: str, sql: str) -> str:
        raw = f"{scope}\x00{_normalize_sql(sql)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _remove_locked(self, key: str) -> None:
        entry = self._index.pop(key, None)
        if entry is None:
            return
        self._total_bytes -= entry.size_bytes
        try:
            entry.path.unlink(missing_ok=True)
        except Exception:
            logger.debug("ResultLake: failed to unlink %s", entry.path, exc_info=True)

    def _evict_to_fit_locked(self) -> None:
        if self._total_bytes <= self.config.max_bytes:
            return
        # Evict lowest-value entries first until under budget.
        for entry in sorted(self._index.values(), key=lambda e: e.value()):
            if self._total_bytes <= self.config.max_bytes:
                break
            self._remove_locked(entry.key)

    def _sweep_orphans(self) -> None:
        """Delete any *.parquet / *.tmp files left by a previous process."""
        for pattern in ("*.parquet", ".*.tmp"):
            for p in self.config.root.glob(pattern):
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    logger.debug("ResultLake: failed to sweep %s", p, exc_info=True)


_LAKE: Optional[ResultLake] = None
_LAKE_LOCK = threading.Lock()


def get_result_lake() -> ResultLake:
    """Lazily build the process-wide ResultLake singleton."""
    global _LAKE
    if _LAKE is None:
        with _LAKE_LOCK:
            if _LAKE is None:
                _LAKE = ResultLake(CacheConfig.from_env())
    return _LAKE

"""Where spill bytes physically live.

This module is the backend seam of the lazy path: `StreamConfig` owns *policy*
(row/byte budgets, chunk size) and delegates *mechanics* — allocating spill
locations, measuring usage, deleting, sweeping orphans — to a `SpillStorage`.
`LocalSpillStorage` is the only implementation today; an S3-compatible backend
would be the second (see the `SpillStorage` docstring for its obligations).
"""

from __future__ import annotations

import abc
import logging
import os
import stat as _stat
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path

logger = logging.getLogger(__name__)


def _ensure_secure_root(root: Path, strict: bool) -> None:
    """Create/verify the spill root. Spill files hold complete query results.

    strict=True (the DEFAULT root under the shared system tempdir): created
    0700 and verified — not a symlink (squatting attack: another user
    pre-creates or symlinks the path and reads every tenant's spills) and
    owned by the current uid.

    strict=False (an explicit BOW_LAZY_DIR): the operator chose the path, and
    legitimate deployments break under the strict rules (root-owned k8s/docker
    volume mount points, symlinks onto a big disk) — and chmod'ing an
    operator-owned dir could strip access other processes rely on. Just create
    it if missing; per-query subdirectories are still created 0700/owned by
    us, which is what actually protects spill contents."""
    if not strict:
        root.mkdir(parents=True, exist_ok=True)
        return
    with suppress(FileExistsError):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(root)
    if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISDIR(st.st_mode):
        raise RuntimeError(
            f"Lazy spill dir {root} is a symlink or not a directory; refusing to "
            "spill query results into it. Set BOW_LAZY_DIR to a private directory."
        )
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise RuntimeError(
            f"Lazy spill dir {root} is owned by another user; refusing to spill "
            "query results into it. Set BOW_LAZY_DIR to a private directory."
        )
    try:
        os.chmod(root, 0o700)
    except OSError:
        logger.debug("Could not chmod spill root %s", root, exc_info=True)


def _restrict_file(path: Path) -> None:
    """chmod a freshly-created spill file to owner-only. Writers create files
    at the process umask (typically 0644 → world-readable query results)."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        logger.debug("Could not chmod spill file %s", path, exc_info=True)


# Spill files older than this are considered orphans (see _sweep_stale_files).
_STALE_AFTER_SECONDS = 24 * 3600
# Re-sweep a root at most this often. Once-per-process is not enough for a
# long-lived server: files orphaned *after* startup would never be reclaimed
# until the next restart.
_SWEEP_INTERVAL_SECONDS = 3600
_last_sweep: dict = {}


def _sweep_stale_files(root: Path) -> None:
    """Best-effort orphan cleanup for the lazy spill dir, at most once per
    _SWEEP_INTERVAL_SECONDS per root. LazyFrame's finalizer deletes its own
    file, but a crashed/killed run never gets there and would leak Parquet
    files forever. Anything older than 24h is long past any live query's
    lifetime, so delete it. Only files matching our own naming pattern are
    touched, and errors are swallowed — this must never break a query."""
    import time

    now = time.monotonic()
    last = _last_sweep.get(root)
    if last is not None and now - last < _SWEEP_INTERVAL_SECONDS:
        return
    _last_sweep[root] = now

    import shutil

    cutoff = time.time() - _STALE_AFTER_SECONDS
    try:
        for f in root.glob("lazy_*.parquet"):  # legacy flat spills
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
            except OSError:
                continue
        # Per-query q_* dirs hold spill parts AND DuckDB temp state; a
        # crashed/killed process orphans the whole dir, so sweep it as a unit.
        for d in root.glob("q_*"):
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue
    except Exception:
        logger.debug("lazy_frame: stale-file sweep of %s failed", root, exc_info=True)


def _release_lazy_resources(con, paths) -> None:
    """Close the DuckDB connection and remove the spill file(s) plus their
    private q_* directory (which also holds DuckDB's temp state). Module-level
    (not a method) so weakref.finalize doesn't hold a reference back to the
    LazyFrame, which would keep it alive forever."""
    import shutil

    if con is not None:
        try:
            con.close()
        except Exception:
            logger.debug("LazyFrame: failed to close duckdb connection", exc_info=True)
    parents = set()
    for p in paths:
        p = Path(p)
        try:
            p.unlink(missing_ok=True)
        except Exception:
            logger.debug("LazyFrame: failed to unlink %s", p, exc_info=True)
        if p.parent.name.startswith("q_"):
            parents.add(p.parent)
    for d in parents:
        shutil.rmtree(d, ignore_errors=True)


class SpillStorage(abc.ABC):
    """Contract a spill backend must satisfy.

    Implementations hand out *local filesystem* paths: DuckDB compute, its
    out-of-core temp state, and the `allowed_directories` confinement that
    sandboxes LLM-generated `.sql()` calls all require local paths. A remote
    backend (e.g. an S3-compatible object store) is therefore expected to
    allocate paths inside a bounded local cache and persist/restore the bytes
    remotely — not to return object-store URLs here.

    Known local-only remnants when adding the first remote backend:
    `LazyFrame`'s finalizer releases files via `_release_lazy_resources`
    directly (a finalizer must not resurrect the frame, but it may capture the
    storage instance), and `LazyFrame.spill_stats()` reads Parquet footers
    straight off the paths.
    """

    root: Path

    @abc.abstractmethod
    def new_spill_path(self) -> Path:
        """Reserve a private location for one query's spill file(s)."""

    @abc.abstractmethod
    def restrict_file(self, path: Path) -> None:
        """Make a freshly-written spill file private to this process's owner."""

    @abc.abstractmethod
    def used_bytes(self) -> int | None:
        """Total bytes currently stored by this backend, or None if unknown."""

    @abc.abstractmethod
    def free_bytes(self) -> int | None:
        """Free capacity left on the underlying medium, or None if unknown."""

    @abc.abstractmethod
    def release(self, paths) -> None:
        """Delete the given spill path(s) and any per-query state around them."""

    @abc.abstractmethod
    def sweep_stale(self) -> None:
        """Best-effort reclamation of spills orphaned by crashed runs."""


class LocalSpillStorage(SpillStorage):
    """Spills live in a secured directory on local disk."""

    def __init__(self, root: Path, strict: bool) -> None:
        self.root = root
        _ensure_secure_root(root, strict)

    @classmethod
    def from_env(cls) -> LocalSpillStorage:
        root = os.environ.get("BOW_LAZY_DIR")
        if root:
            return cls(Path(root), strict=False)
        # Per-uid default: a fixed name under world-writable /tmp is the
        # classic shared-tempdir pitfall (pre-creation/symlink squatting,
        # other users reading spilled query results).
        suffix = f"_{os.getuid()}" if hasattr(os, "getuid") else ""
        return cls(Path(tempfile.gettempdir()) / f"bow_lazy{suffix}", strict=True)

    def new_spill_path(self) -> Path:
        """Reserve a spill location inside a PRIVATE per-query subdirectory
        (root/q_<hex>/lazy_<hex>.parquet, dir mode 0700).

        The subdirectory is the isolation boundary: the LazyFrame's DuckDB
        connection is confined (allowed_directories) to it, so sandboxed code
        holding one frame cannot glob-read or COPY-overwrite OTHER queries'
        in-flight spills — which it could when every query spilled flat into
        one shared root. DuckDB's own temp state lives here too, so releasing
        the frame (or sweeping a crashed query's leftovers) is one rmtree."""
        q_dir = self.root / f"q_{uuid.uuid4().hex}"
        q_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return q_dir / f"lazy_{uuid.uuid4().hex}.parquet"

    def restrict_file(self, path: Path) -> None:
        _restrict_file(path)

    def used_bytes(self) -> int | None:
        try:
            return sum(f.stat().st_size for f in self.root.rglob("*") if f.is_file())
        except Exception:
            return None

    def free_bytes(self) -> int | None:
        import shutil

        try:
            return shutil.disk_usage(
                self.root.parent if not self.root.exists() else self.root
            ).free
        except Exception:
            return None

    def release(self, paths) -> None:
        _release_lazy_resources(None, paths)

    def sweep_stale(self) -> None:
        _sweep_stale_files(self.root)

"""Encrypted DuckDB artifacts for BOW custom tables.

A custom table is materialized into a **DuckDB database file encrypted at rest**
rather than a bare Parquet file. That is a security boundary, not just at-rest
compliance:

    The agent's generated Python runs with `pd` injected into its namespace
    (`code_execution.py`), `pandas` is not in FORBIDDEN_MODULES, and while
    `open` is blocked, pandas' own IO does not use it. A plain artifact could
    therefore be read with `pd.read_parquet('<path>')`, bypassing the view
    catalog entirely. pandas cannot open an encrypted DuckDB file, and the key
    never enters the sandbox namespace.

Each artifact gets its own random key, stored Fernet-encrypted on the
ConnectionTable row using the same `settings.bow_config.encryption_key` that
already backs SMTP and external-platform secrets.
"""

import asyncio
import base64
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Optional

import duckdb
from cryptography.fernet import Fernet
from filelock import FileLock, Timeout

from app.errors import AppError
from app.settings.config import settings

logger = logging.getLogger(__name__)

# Artifacts live beside the other on-disk caches (qvd_cache, filecache).
_ARTIFACT_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "uploads" / "fast"


def _fernet() -> Fernet:
    return Fernet(settings.bow_config.encryption_key)


def new_artifact_key() -> str:
    """A fresh per-artifact DuckDB encryption key."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def encrypt_key(raw_key: str) -> str:
    return _fernet().encrypt(raw_key.encode()).decode()


def decrypt_key(enc_key: str) -> str:
    return _fernet().decrypt(enc_key.encode()).decode()


def artifact_dir(connection_id: str) -> Path:
    d = _ARTIFACT_ROOT / str(connection_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_artifact_path(connection_id: str) -> Path:
    """Opaque uuid filename.

    Deliberately NOT a content hash or anything derived from the query/table
    name: a path that can be derived from inside the sandbox is a path that can
    be attempted. Encryption is the real boundary, this is depth.
    """
    return artifact_dir(connection_id) / f"{uuid.uuid4().hex}.db"


def connect_encrypted(path: str | Path, key: str, read_only: bool = False):
    """Open an encrypted DuckDB database.

    The key is attached to an in-memory root connection and the artifact is
    ATTACHed, because DuckDB takes ENCRYPTION_KEY as an ATTACH option.
    """
    con = duckdb.connect(database=":memory:")
    try:
        ro = ", READ_ONLY" if read_only else ""
        con.execute(
            f"ATTACH '{str(path)}' AS artifact (ENCRYPTION_KEY '{key}'{ro})"
        )
        con.execute("USE artifact")
        return con
    except Exception:
        con.close()
        raise


def delete_artifact(path: Optional[str]) -> None:
    """Best-effort removal of an artifact file."""
    if not path:
        return
    for candidate in (path, str(path) + ".wal"):
        try:
            os.unlink(candidate)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("fast.artifact.delete_failed", extra={"path": candidate, "error": str(e)})


def artifact_size(path: Optional[str]) -> int:
    if not path:
        return 0
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


@contextmanager
def try_refresh_lock(connection_id: str):
    """Coordinate writers/GC across processes sharing the artifact volume.

    Keep the inode across releases; process exit releases the kernel lock. Use
    the pinned filelock implementation with no soft-lock fallback: a soft lock
    cannot provide the crash-release guarantee required by artifact maintenance.
    """
    lock = FileLock(
        artifact_dir(connection_id) / ".refresh.lock",
        preserve_lock_file=True,
        fallback_to_soft=False,
        thread_local=False,
    )
    try:
        lock.acquire(blocking=False)
    except Timeout:
        yield False
        return
    try:
        yield True
    finally:
        lock.release()


@asynccontextmanager
async def refresh_lock(connection_id: str, timeout: float | None = None):
    """Serialize refreshes of one connection across workers sharing the volume.

    A refresh is a full scan against the source, so queued callers wait their
    turn (as the in-process lock this replaced did) rather than failing: a
    scheduled tick for a sibling table must run after the current one, not be
    dropped for a whole interval. Waiting backs off exponentially and never
    blocks the event loop. Pass ``timeout`` to bound the wait; expiry raises the
    localized 409 conflict response.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    delay = 0.05
    while True:
        with try_refresh_lock(connection_id) as acquired:
            if acquired:
                yield
                return
        if deadline is None:
            await asyncio.sleep(delay)
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Existing localized conflict response; no hard-coded UI message.
                raise AppError.conflict()
            await asyncio.sleep(min(delay, remaining))
        delay = min(delay * 2, 1.0)


def protected_paths(references: list[str]) -> set[tuple[str, str]]:
    """Identify referenced artifacts by (connection directory, file name).

    Artifact names are per-connection UUIDs, so this identity is exact without
    depending on the absolute prefix a row was written under. Comparing fully
    resolved paths would treat every served artifact as unreferenced the moment
    the volume is reached from a different mount point or install directory
    (a relocated release, a bind mount), and the sweep would delete them all.
    Each referenced WAL sidecar is protected with its artifact.
    """
    protected: set[tuple[str, str]] = set()
    for ref in references:
        if not ref:
            continue
        path = Path(ref)
        protected.add((path.parent.name, path.name))
        protected.add((path.parent.name, path.name + ".wal"))
    return protected


def prune_unreferenced(connection_id: str, protected: set[tuple[str, str]], before: float) -> dict:
    """Delete only old artifact files; caller must hold the refresh lock.

    Preserve references from *all* rows (including soft-deleted ones), their WAL
    sidecars, recent files, symlinks, and unknown file types. No recursive walk:
    artifact directories are flat and GC must never traverse outside its volume.
    """
    directory = artifact_dir(connection_id)
    deleted = reclaimed = 0
    for path in directory.iterdir():
        try:
            if path.is_symlink() or not path.is_file():
                continue
            if path.suffix not in {".db", ".tmp", ".wal"}:
                continue
            if (directory.name, path.name) in protected:
                continue
            info = path.stat()
            if info.st_mtime >= before:
                continue
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning("fast.artifact.prune_failed", extra={"path": str(path)}, exc_info=True)
            continue
        deleted += 1
        reclaimed += info.st_size
    return {"deleted": deleted, "reclaimed_bytes": reclaimed}

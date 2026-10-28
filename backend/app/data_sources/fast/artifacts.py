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
import fcntl
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Optional

import duckdb
from cryptography.fernet import Fernet

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

    The inode must persist: unlinking a lock file lets another process lock a
    different inode while the first owner is still working. A crashed process
    releases flock automatically. All replicas sharing artifacts must share this
    directory too; unrelated volumes contain independent files.
    """
    path = artifact_dir(connection_id) / ".refresh.lock"
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@asynccontextmanager
async def refresh_lock(connection_id: str):
    """Wait without blocking the event loop; cancellation closes the handle."""
    while True:
        with try_refresh_lock(connection_id) as acquired:
            if acquired:
                yield
                return
        await asyncio.sleep(0.1)


def prune_unreferenced(connection_id: str, references: list[str], before: float) -> dict:
    """Delete only old artifact files; caller must hold the refresh lock.

    Preserve references from *all* rows (including soft-deleted ones), their WAL
    sidecars, recent files, symlinks, and unknown file types. No recursive walk:
    artifact directories are flat and GC must never traverse outside its volume.
    """
    directory = artifact_dir(connection_id)
    protected = {str(Path(p).resolve()) for p in references if p}
    protected |= {p + ".wal" for p in protected}
    deleted = reclaimed = 0
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix not in {".db", ".tmp", ".wal"}:
            continue
        if str(path.resolve()) in protected:
            continue
        try:
            info = path.stat()
            if info.st_mtime >= before:
                continue
            path.unlink()
        except FileNotFoundError:
            continue
        deleted += 1
        reclaimed += info.st_size
    return {"deleted": deleted, "reclaimed_bytes": reclaimed}

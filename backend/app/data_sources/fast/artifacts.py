"""Encrypted DuckDB artifacts for BOW custom queries.

A custom query is materialized into a **DuckDB database file encrypted at rest**
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

import base64
import logging
import os
import secrets
import uuid
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
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("fast.artifact.delete_failed", extra={"path": path, "error": str(e)})


def artifact_size(path: Optional[str]) -> int:
    if not path:
        return 0
    try:
        return os.path.getsize(path)
    except OSError:
        return 0

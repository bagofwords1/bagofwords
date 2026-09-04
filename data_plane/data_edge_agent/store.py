"""Persistent store for admin-UI connections.

Connections added or edited through the agent's local admin UI are persisted
here so they survive a restart. Both the non-secret config (host, port,
database) and the credentials are stored in **cleartext**.

There is no at-rest encryption by design: securing the file — and the host it
runs on — is the site owner's responsibility. The store still writes the file
`0600` and keeps it under the agent's data dir, but that is basic hygiene, not
a security boundary. Put it on trusted, access-controlled storage.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from .config import ConnectionConfig

logger = logging.getLogger(__name__)

_STORE_VERSION = 1


class StoreError(Exception):
    """A store operation could not complete (unreadable or corrupt file)."""


class ConnectionStore:
    """Reads and writes UI-managed connections to a JSON file on disk."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    # -- persistence ------------------------------------------------------

    def _read_raw(self) -> dict[str, Any]:
        if not self._path.is_file():
            return {"version": _STORE_VERSION, "connections": []}
        try:
            return json.loads(self._path.read_text()) or {"version": _STORE_VERSION, "connections": []}
        except (json.JSONDecodeError, OSError) as e:
            raise StoreError(f"cannot read store {self._path}: {e}") from e

    def _write_raw(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace: write a temp file in the same dir and rename, so a
        # crash mid-write never leaves a half-written store. 0600 because the
        # file holds credentials in cleartext.
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), prefix=".store-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- (de)serialization ------------------------------------------------

    def _encode(self, conn: ConnectionConfig) -> dict[str, Any]:
        return {
            "name": conn.name,
            "type": conn.type,
            "label": conn.label,
            "config": conn.config or {},
            "credentials": conn.credentials or {},
            "query_timeout_seconds": conn.query_timeout_seconds,
        }

    def _decode(self, row: dict[str, Any]) -> ConnectionConfig:
        return ConnectionConfig(
            name=row["name"],
            type=row["type"],
            label=row.get("label"),
            config=row.get("config") or {},
            credentials=row.get("credentials") or {},
            query_timeout_seconds=row.get("query_timeout_seconds"),
        )

    # -- public API -------------------------------------------------------

    def list(self) -> list[ConnectionConfig]:
        return [self._decode(r) for r in self._read_raw().get("connections", [])]

    def get(self, name: str) -> Optional[ConnectionConfig]:
        return next((c for c in self.list() if c.name == name), None)

    def upsert(self, conn: ConnectionConfig) -> None:
        data = self._read_raw()
        rows = [r for r in data.get("connections", []) if r.get("name") != conn.name]
        rows.append(self._encode(conn))
        data["version"] = _STORE_VERSION
        data["connections"] = rows
        self._write_raw(data)

    def delete(self, name: str) -> bool:
        data = self._read_raw()
        rows = data.get("connections", [])
        remaining = [r for r in rows if r.get("name") != name]
        if len(remaining) == len(rows):
            return False
        data["connections"] = remaining
        self._write_raw(data)
        return True

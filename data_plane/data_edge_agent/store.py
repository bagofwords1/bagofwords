"""Persistent, credential-encrypting store for admin-UI connections.

The whole reason the admin UI is agent-local (design C4) is that credentials
must be entered and kept on the customer's own network and never cross the
tunnel. This store is where the UI writes them. Non-secret connection detail
(host, port, database) is stored in clear so an operator can read the file and
see what is configured; the `credentials` dict is Fernet-encrypted at rest.

Key handling, in order of preference:
  1. `BOW_EDGE_AGENT_STORE_KEY` / the config's `store_key` — supply from the
     deployment's own secret management. This is the intended path.
  2. A key file beside the store (`<store>.key`, mode 0600), generated on first
     run. Convenient for a single-box install; offers protection against casual
     disclosure of the store, not against someone who can read both files.

The store is the source of truth for UI-managed connections. Connections
defined in the YAML config still load, but a UI edit to the same name lands
here and wins (see main.load_connections).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken

from .config import ConnectionConfig

logger = logging.getLogger(__name__)

_STORE_VERSION = 1


class StoreError(Exception):
    """A store operation could not complete (bad key, unreadable file)."""


class ConnectionStore:
    """Reads and writes UI-managed connections to a JSON file on disk.

    Credentials are encrypted per connection so the on-disk file can be read
    without exposing them. Everything else is plaintext by design.
    """

    def __init__(self, path: str | Path, key: Optional[str] = None) -> None:
        self._path = Path(path)
        self._fernet = Fernet(self._resolve_key(key))

    # -- key --------------------------------------------------------------

    def _key_file(self) -> Path:
        return self._path.with_suffix(self._path.suffix + ".key")

    def _resolve_key(self, key: Optional[str]) -> bytes:
        if key:
            return key.encode() if isinstance(key, str) else key
        kf = self._key_file()
        if kf.is_file():
            return kf.read_bytes().strip()
        # First run with no supplied key: generate and persist one, tightly.
        generated = Fernet.generate_key()
        kf.parent.mkdir(parents=True, exist_ok=True)
        # Write 0600 from the start rather than chmod-after, so the key is never
        # briefly world-readable between create and chmod.
        fd = os.open(str(kf), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, generated)
        finally:
            os.close(fd)
        logger.warning(
            "edge_agent.store.key_generated",
            extra={
                "key_file": str(kf),
                "hint": "supply BOW_EDGE_AGENT_STORE_KEY from secret management "
                        "instead of relying on this generated key file",
            },
        )
        return generated

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
        # file still carries (encrypted) secrets and the config keys around them.
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
        creds_json = json.dumps(conn.credentials or {}).encode()
        return {
            "name": conn.name,
            "type": conn.type,
            "label": conn.label,
            "config": conn.config or {},
            "credentials_enc": self._fernet.encrypt(creds_json).decode(),
            "query_timeout_seconds": conn.query_timeout_seconds,
        }

    def _decode(self, row: dict[str, Any]) -> ConnectionConfig:
        creds: dict[str, Any] = {}
        token = row.get("credentials_enc")
        if token:
            try:
                creds = json.loads(self._fernet.decrypt(token.encode()).decode())
            except InvalidToken as e:
                # A wrong key must not silently drop credentials into a working
                # connection with none — that would fail confusingly at query
                # time. Surface it loudly instead.
                raise StoreError(
                    f"cannot decrypt credentials for connection {row.get('name')!r}: "
                    "the store key does not match the one that wrote this store"
                ) from e
        return ConnectionConfig(
            name=row["name"],
            type=row["type"],
            label=row.get("label"),
            config=row.get("config") or {},
            credentials=creds,
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

"""The object-store contract every remote backend implements.

Backends are synchronous and work on whole local files: callers (the facade in
``app.services.storage``) handle key mapping, atomic replace, freshness and the
async wrappers. Keys are relative (``files/<uuid>_report.xlsx``); the backend
applies the configured prefix.
"""
from __future__ import annotations

import abc
from collections.abc import Iterator


class StorageError(RuntimeError):
    """An object-store call failed (credentials, network, permissions)."""


class ObjectStore(abc.ABC):
    name: str = "object-store"

    def __init__(self, prefix: str = ""):
        self.prefix = prefix or ""

    def _k(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def _strip(self, full_key: str) -> str:
        return full_key[len(self.prefix):] if self.prefix and full_key.startswith(self.prefix) else full_key

    @abc.abstractmethod
    def upload(self, key: str, local_path: str, content_type: str | None = None) -> str | None:
        """Upload a local file; returns the new ETag when known."""

    @abc.abstractmethod
    def download(self, key: str, local_path: str) -> str | None:
        """Download into ``local_path``; returns the ETag, or None if the key is missing."""

    @abc.abstractmethod
    def etag(self, key: str) -> str | None:
        """Current ETag, or None if the key does not exist."""

    @abc.abstractmethod
    def delete(self, key: str) -> None:
        """Delete one key; missing keys are not an error."""

    @abc.abstractmethod
    def list(self, prefix: str) -> Iterator[str]:
        """Yield every key (relative, without the configured prefix) under ``prefix``."""

    def list_detailed(self, prefix: str) -> Iterator[tuple[str, float | None]]:
        """Like ``list`` with each object's last-modified time (epoch seconds)."""
        for key in self.list(prefix):
            yield key, None

    def delete_prefix(self, prefix: str) -> int:
        n = 0
        for key in list(self.list(prefix)):
            self.delete(key)
            n += 1
        return n

    def describe(self) -> str:
        return self.name

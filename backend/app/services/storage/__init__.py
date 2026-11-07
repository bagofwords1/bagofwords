"""Durable file storage: local disk (default), S3-compatible, or Azure Blob.

The app keeps reading and writing real files under ``backend/uploads/`` — the
code sandbox, pandas/openpyxl, Playwright and LibreOffice all need a path. With
``storage.backend`` set to ``s3`` or ``azure`` the object store becomes the
source of truth and ``uploads/`` a per-pod cache:

* after writing a durable file, call ``persist(path)`` (or ``apersist``);
* before reading one, call ``ensure_local(path)`` — it downloads the file when
  this pod does not have it yet (a new replica, a restarted pod);
* when deleting one, also call ``discard(path)``.

By default ``ensure_local`` also checks the remote ETag of a file it already
has, because several files are rewritten in place under the same name
(connector copies, thumbnails, PDFs, logos, precompiled JS): a pod never serves
a copy another pod has since replaced or deleted. Pass ``fresh=False`` to skip
that round trip for files known to be write-once.

With the default ``local`` backend every function is a no-op and
``ensure_local`` just reports whether the file exists, so behavior is exactly
what it was before object storage existed.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import os
import threading
import uuid

from .base import ObjectStore, StorageError
from .keys import UPLOADS_ROOT, PathLike, key_for, prefix_for

logger = logging.getLogger(__name__)

__all__ = [
    "StorageError", "ObjectStore", "UPLOADS_ROOT", "key_for",
    "get_store", "set_store", "reset_store", "enabled", "describe", "check", "ensure_layout",
    "persist", "persist_tree", "ensure_local", "ensure_local_tree",
    "discard", "discard_tree", "remote_entries",
    "apersist", "apersist_tree", "aensure_local", "aensure_local_tree",
    "adiscard", "adiscard_tree",
]

_UNSET = object()
_store = _UNSET
_store_lock = threading.Lock()
# key -> ETag of the object this process last uploaded or downloaded, i.e. the
# version the local copy is known to match.
_known: dict[str, str | None] = {}


def _build_store() -> ObjectStore | None:
    from app.settings.config import settings

    cfg = getattr(settings.bow_config, "storage", None) if settings.bow_config else None
    if cfg is None or cfg.backend == "local":
        return None
    if cfg.backend == "s3":
        from .s3 import S3Store
        return S3Store(cfg.s3, prefix=cfg.prefix)
    if cfg.backend == "azure":
        from .azure_blob import AzureBlobStore
        return AzureBlobStore(cfg.azure, prefix=cfg.prefix)
    raise StorageError(f"Unknown storage backend {cfg.backend!r}")


def get_store() -> ObjectStore | None:
    """The configured remote store, or None for the local backend."""
    global _store
    if _store is _UNSET:
        with _store_lock:
            if _store is _UNSET:
                _store = _build_store()
    return _store  # type: ignore[return-value]


def set_store(store: ObjectStore | None) -> None:
    """Swap the active store (tests, scripts). ``None`` = local backend."""
    global _store
    with _store_lock:
        _store = store
        _known.clear()


def reset_store() -> None:
    """Forget the active store; the next call rebuilds it from settings."""
    global _store
    with _store_lock:
        _store = _UNSET
        _known.clear()


def enabled() -> bool:
    return get_store() is not None


def describe() -> str:
    store = get_store()
    return store.describe() if store else f"local ({UPLOADS_ROOT})"


# Folders writers expect to exist. The image pre-creates some of them, but a
# volume mounted over uploads/ (a Kubernetes emptyDir or PVC, a fresh Docker
# volume) hides them.
_LAYOUT = ("files", "thumbnails", "pptx", "pptx_previews", "pdfs", "branding", "avatars",
           "demo_data", "git_state", "artifact_js", "filecache", "fast")


def ensure_layout() -> None:
    """Create the standard uploads/ sub-folders (idempotent; run at startup)."""
    for name in _LAYOUT:
        os.makedirs(os.path.join(str(UPLOADS_ROOT), name), exist_ok=True)


def check() -> str:
    """Round-trip a canary object; raises StorageError with a clear message."""
    store = get_store()
    if store is None:
        return describe()
    key = f"_healthcheck/{uuid.uuid4().hex}.txt"
    tmp_dir = os.path.join(str(UPLOADS_ROOT), ".storage-tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    src = os.path.join(tmp_dir, f"{uuid.uuid4().hex}.src")
    dst = os.path.join(tmp_dir, f"{uuid.uuid4().hex}.dst")
    try:
        with open(src, "wb") as fh:
            fh.write(b"bow-storage-check")
        try:
            store.upload(key, src, "text/plain")
            if store.download(key, dst) is None:
                raise StorageError("object written but not readable back")
            with open(dst, "rb") as fh:
                if fh.read() != b"bow-storage-check":
                    raise StorageError("object read back with different content")
            store.delete(key)
        except StorageError as e:
            raise StorageError(
                f"Storage backend {store.describe()} failed its startup check "
                f"(needs read, write, delete and list on the prefix): {e}"
            ) from e
    finally:
        for p in (src, dst):
            with contextlib.suppress(OSError):
                os.remove(p)
    return store.describe()


# --- sync API -------------------------------------------------------------------


def persist(path: PathLike, content_type: str | None = None) -> bool:
    """Upload a durable local file. Raises StorageError when the upload fails,
    so a request never reports success for a file only this pod holds."""
    store = get_store()
    key = key_for(path) if store else None
    if store is None or key is None:
        return False
    p = os.fspath(path)
    if not os.path.isfile(p):
        raise StorageError(f"cannot persist {p!r}: not a file")
    ctype = content_type or mimetypes.guess_type(p)[0]
    _known[key] = store.upload(key, p, ctype)
    return True


def persist_tree(directory: PathLike) -> int:
    """Upload every file below a directory (e.g. a deck's preview images)."""
    store = get_store()
    if store is None or prefix_for(directory) is None:
        return 0
    n = 0
    for root, dirs, files in os.walk(os.fspath(directory)):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if persist(os.path.join(root, name)):
                n += 1
    return n


def _download(store: ObjectStore, key: str, dest: str) -> bool:
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    part = os.path.join(os.path.dirname(dest) or ".", f".{os.path.basename(dest)}.{uuid.uuid4().hex}.part")
    try:
        etag = store.download(key, part)
        if etag is None and not os.path.exists(part):
            return False
        if etag is None:
            # Missing remotely (backend may still have created an empty file).
            return False
        os.replace(part, dest)
        _known[key] = etag
        return True
    finally:
        with contextlib.suppress(OSError):
            os.remove(part)


def ensure_local(path: PathLike, *, fresh: bool = True) -> bool:
    """Make sure ``path`` exists locally, fetching it from the store if needed.

    Returns whether the file exists afterwards. Never raises for a missing or
    unreachable object: callers keep their usual "file not found" handling, and
    the failure is logged.
    """
    p = os.fspath(path) if path is not None else ""
    if not p:
        return False
    store = get_store()
    key = key_for(p) if store else None
    if store is None or key is None:
        return os.path.isfile(p)
    try:
        if os.path.isfile(p):
            if not fresh:
                return True
            remote = store.etag(key)
            if remote is None:
                # Deleted (or never uploaded) elsewhere: drop the stale copy
                # unless this process wrote it and it simply is not uploaded.
                if key in _known:
                    _known.pop(key, None)
                    with contextlib.suppress(OSError):
                        os.remove(p)
                    return False
                return True
            if _known.get(key) == remote:
                return True
        return _download(store, key, p)
    except StorageError as e:
        logger.warning("storage: could not fetch %s from %s: %s", key, store.describe(), e)
        return os.path.isfile(p)


def ensure_local_tree(directory: PathLike) -> bool:
    """Fetch every object under a directory that is missing locally.

    Returns whether the directory holds any file afterwards.
    """
    d = os.fspath(directory)
    store = get_store()
    prefix = prefix_for(d) if store else None
    if store is not None and prefix is not None:
        try:
            for key in store.list(prefix):
                rel = key[len(prefix):]
                if not rel or any(part in ("", ".", "..") or part.startswith(".") for part in rel.split("/")):
                    continue
                local = os.path.join(d, *rel.split("/"))
                if not os.path.isfile(local):
                    _download(store, key, local)
        except StorageError as e:
            logger.warning("storage: could not list %s on %s: %s", prefix, store.describe(), e)
    if not os.path.isdir(d):
        return False
    return any(any(not f.startswith(".") for f in files) for _root, _dirs, files in os.walk(d))


def remote_entries(directory: PathLike) -> list[tuple[str, float | None]]:
    """(local path, last-modified epoch) of every object stored under a
    directory, whether or not this pod holds a copy. For garbage collection
    that must also see files only other pods wrote. Empty on local storage."""
    store = get_store()
    prefix = prefix_for(directory) if store else None
    if store is None or prefix is None:
        return []
    d = os.fspath(directory)
    out = []
    for key, modified in store.list_detailed(prefix):
        rel = key[len(prefix):]
        if rel and _clean_rel(rel):
            out.append((os.path.join(d, *rel.split("/")), modified))
    return out


def _clean_rel(rel: str) -> bool:
    return not any(part in ("", ".", "..") or part.startswith(".") for part in rel.split("/"))


def discard(path: PathLike) -> None:
    """Delete a durable file from the store (the caller removes the local copy).
    Best effort: a failure is logged, never raised, like a local unlink."""
    store = get_store()
    key = key_for(path) if store else None
    if store is None or key is None:
        return
    _known.pop(key, None)
    try:
        store.delete(key)
    except StorageError as e:
        logger.warning("storage: could not delete %s on %s: %s", key, store.describe(), e)


def discard_tree(directory: PathLike) -> None:
    store = get_store()
    prefix = prefix_for(directory) if store else None
    if store is None or prefix is None:
        return
    for k in [k for k in _known if k.startswith(prefix)]:
        _known.pop(k, None)
    try:
        store.delete_prefix(prefix)
    except StorageError as e:
        logger.warning("storage: could not delete %s on %s: %s", prefix, store.describe(), e)


# --- async API (same semantics; network I/O off the event loop) -----------------


async def apersist(path: PathLike, content_type: str | None = None) -> bool:
    if not enabled():
        return False
    return await asyncio.to_thread(persist, path, content_type)


async def apersist_tree(directory: PathLike) -> int:
    if not enabled():
        return 0
    return await asyncio.to_thread(persist_tree, directory)


async def aensure_local(path: PathLike, *, fresh: bool = True) -> bool:
    if not enabled():
        return ensure_local(path)
    return await asyncio.to_thread(ensure_local, path, fresh=fresh)


async def aensure_local_tree(directory: PathLike) -> bool:
    if not enabled():
        return ensure_local_tree(directory)
    return await asyncio.to_thread(ensure_local_tree, directory)


async def adiscard(path: PathLike) -> None:
    if enabled():
        await asyncio.to_thread(discard, path)


async def adiscard_tree(directory: PathLike) -> None:
    if enabled():
        await asyncio.to_thread(discard_tree, directory)

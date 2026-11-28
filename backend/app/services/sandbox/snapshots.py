"""Snapshots of a sandbox's workspace, kept on BoW's side.

A workspace lives on one compute's own disk. A snapshot is a copy of it (a
gzip tar) in BoW's file store, so that losing the compute, or moving the
sandbox off it, does not lose the workspace. One snapshot per sandbox: a new
one replaces the old.

The files are under ``uploads/sandbox_snapshots/`` and follow the file
store's rules (mirrored to the object store when one is configured). A
snapshot holds everything in the workspace, including anything a run left
there; it is deleted when its sandbox is deleted on purpose.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, AsyncIterator, Dict, Optional

from app.services.sandbox.drivers.base import SandboxDriver, SandboxHandle

logger = logging.getLogger(__name__)

AREA = "sandbox_snapshots"
CHUNK_BYTES = 1024 * 1024


def _root() -> str:
    from app.services.storage.keys import UPLOADS_ROOT

    return os.path.join(str(UPLOADS_ROOT), AREA)


def path_for(sandbox_id: str) -> str:
    return os.path.join(_root(), f"{sandbox_id}.tar.gz")


async def take(driver: SandboxDriver, handle: SandboxHandle, *, max_bytes: int) -> Dict[str, Any]:
    """Copy the workspace into the file store. Returns ``{"path", "bytes", "at"}``.
    A snapshot that fails or is too large leaves the previous one in place."""
    import aiofiles

    from app.services import storage

    final = path_for(handle.sandbox_id)
    partial = final + ".partial"
    os.makedirs(os.path.dirname(final), exist_ok=True)
    try:
        async with aiofiles.open(partial, "wb") as out:
            size = await driver.export_workspace(handle, out.write, max_bytes=max_bytes)
        os.replace(partial, final)
    finally:
        if os.path.exists(partial):
            os.remove(partial)
    try:
        await storage.apersist(final, "application/gzip")
    except Exception:  # noqa: BLE001 - the local copy is still a snapshot
        logger.warning("could not copy sandbox snapshot %s to the object store", handle.sandbox_id)
    return {"path": final, "bytes": int(size), "at": datetime.utcnow().isoformat()}


async def chunks(snapshot: Dict[str, Any]) -> AsyncIterator[bytes]:
    """The snapshot's bytes, fetched from the object store first if needed."""
    import aiofiles

    from app.services import storage

    path = str(snapshot.get("path") or "")
    await storage.aensure_local(path)
    async with aiofiles.open(path, "rb") as source:
        while True:
            chunk = await source.read(CHUNK_BYTES)
            if not chunk:
                return
            yield chunk


async def available(snapshot: Optional[Dict[str, Any]]) -> bool:
    if not snapshot or not snapshot.get("path"):
        return False
    from app.services import storage

    try:
        await storage.aensure_local(str(snapshot["path"]))
    except Exception:  # noqa: BLE001
        pass
    return os.path.exists(str(snapshot["path"]))


async def discard(snapshot: Optional[Dict[str, Any]]) -> None:
    if not snapshot or not snapshot.get("path"):
        return
    from app.services import storage

    path = str(snapshot["path"])
    # Never outside the snapshot folder, whatever a stored value says.
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(_root()):
        return
    try:
        await storage.adiscard(path)
    except Exception:  # noqa: BLE001
        logger.warning("could not remove sandbox snapshot %s from the object store", path)
    if os.path.exists(path):
        os.remove(path)

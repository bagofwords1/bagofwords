"""Map local paths under ``uploads/`` to object-store keys and back.

Every durable file the app writes lives under ``backend/uploads/<area>/...``
(written either cwd-relative as ``uploads/...`` or anchored on the backend
directory). The object key is that path relative to ``uploads/``, so the DB
values (``File.path`` = ``uploads/files/<uuid>_<name>``, thumbnail and preview
paths) keep working unchanged: the local tree is simply a cache of the bucket.
"""
from __future__ import annotations

import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]
UPLOADS_ROOT = BACKEND_DIR / "uploads"

# Areas that never leave the local disk, because losing them costs only time:
#   repo_cache   read-only git mirrors of a remote (need a real working tree)
#   pdfs         re-rendered for every export and served by the same request
#   artifact_js  content-addressed compile cache, rebuilt on a miss
#   filecache    version-keyed read cache that fails open to a live read
# Everything else under uploads/ is mirrored to the object store.
LOCAL_ONLY_AREAS = frozenset({"repo_cache", "pdfs", "artifact_js", "filecache", ".storage-tmp"})

PathLike = str | os.PathLike


def _roots() -> list[str]:
    roots = [os.path.normpath(str(UPLOADS_ROOT))]
    cwd_root = os.path.normpath(os.path.join(os.getcwd(), "uploads"))
    if cwd_root not in roots:
        roots.append(cwd_root)
    return roots


def key_for(path: PathLike) -> str | None:
    """The object key for a local path, or None when the path is not managed.

    Unmanaged: outside uploads/, the uploads root itself, a local-only area,
    or a transient name (dot-files such as in-flight ``.part`` downloads).
    """
    if path is None or str(path) == "":
        return None
    p = os.path.normpath(os.path.abspath(os.fspath(path)))
    for root in _roots():
        if p == root or not p.startswith(root + os.sep):
            continue
        rel = os.path.relpath(p, root)
        parts = rel.split(os.sep)
        if not parts or ".." in parts or parts[0] in LOCAL_ONLY_AREAS:
            return None
        if any(part.startswith(".") for part in parts):
            return None
        return "/".join(parts)
    return None


def prefix_for(directory: PathLike) -> str | None:
    """The key prefix (ending in '/') for a local directory under uploads/."""
    key = key_for(os.path.join(os.fspath(directory), "_"))
    if key is None or key == "_":
        return None
    return key[: -len("_")]

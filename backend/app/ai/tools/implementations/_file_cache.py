"""Version-keyed content cache for file reads.

Lets a repeated read of an *unchanged* file skip the expensive work — source
download, document-text extraction, and (for scanned PDFs) page rendering —
by keying on a cheap source version token (mtime+size for network_dir, ETag for
s3). Disk-backed under ``uploads/filecache`` (same storage semantics as
uploaded/materialized files).

SECURITY: this cache is only ever populated/served for system-identity,
glob-enforced connections (the caller gates on that), and the version token is
computed via the client's scope-enforcing resolver — so a cache hit is still
access-checked and never bypasses a per-user ACL.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_CACHE_ROOT = Path("uploads/filecache")
# Bound what we persist so the cache can't be blown up by a giant text file.
_MAX_TEXT_CHARS = 400_000


def _entry_dir(connection_id: str, file_id: str) -> Path:
    h = hashlib.sha256(f"{connection_id}\x00{file_id}".encode("utf-8")).hexdigest()[:24]
    return _CACHE_ROOT / str(connection_id) / h


def read(connection_id: str, file_id: str, version: str) -> Optional[Dict[str, Any]]:
    """Return the cached entry iff it exists AND matches this exact version.

    The returned dict carries the stored ``rendered`` payload, ``pages_total``,
    and ``image_bytes`` (a list of PNG byte strings). Returns None on a miss,
    a stale version, or any read error (fail-open → live read).
    """
    if not (connection_id and file_id and version):
        return None
    d = _entry_dir(connection_id, file_id)
    try:
        meta_p = d / "meta.json"
        if not meta_p.exists():
            return None
        meta = json.loads(meta_p.read_text())
        if meta.get("version") != version:
            return None  # stale — caller re-reads and overwrites
        image_bytes: List[bytes] = []
        for name in meta.get("images", []):
            p = d / name
            if p.exists():
                image_bytes.append(p.read_bytes())
        meta["image_bytes"] = image_bytes
        return meta
    except Exception as e:
        logger.info("file cache read failed for %s/%s: %s", connection_id, file_id, e)
        return None


def write(
    connection_id: str,
    file_id: str,
    version: str,
    *,
    rendered: Dict[str, Any],
    image_pngs: Optional[List[bytes]] = None,
    pages_total: Optional[int] = None,
) -> None:
    """Persist the rendered payload + any page images for this version, replacing
    any previous version. Best-effort — a failure here never breaks the read."""
    if not (connection_id and file_id and version):
        return
    d = _entry_dir(connection_id, file_id)
    try:
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
        img_names: List[str] = []
        for i, png in enumerate(image_pngs or []):
            name = f"p{i + 1}.png"
            (d / name).write_bytes(png)
            img_names.append(name)
        # Trim large text/csv payloads before persisting.
        stored = dict(rendered or {})
        for k in ("text", "csv"):
            if isinstance(stored.get(k), str) and len(stored[k]) > _MAX_TEXT_CHARS:
                stored[k] = stored[k][:_MAX_TEXT_CHARS]
                stored["truncated"] = True
        meta = {
            "version": version,
            "rendered": stored,
            "images": img_names,
            "pages_total": pages_total,
        }
        (d / "meta.json").write_text(json.dumps(meta))
    except Exception as e:
        logger.info("file cache write failed for %s/%s: %s", connection_id, file_id, e)

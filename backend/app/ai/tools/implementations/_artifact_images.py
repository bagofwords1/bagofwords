"""Shared image loading for artifact tools.

The pptx sandbox has no filesystem access, so any art a deck places has to be
resolved server-side and handed to the executor as bytes. Both create_artifact
and edit_artifact need this, hence the shared home.
"""

import base64
import logging
from pathlib import Path
from typing import Any, Dict, List

import aiofiles
from sqlalchemy import select

from app.models.file import File

logger = logging.getLogger(__name__)


async def load_image_bytes(db, included_files: List[Dict[str, Any]]) -> Dict[str, bytes]:
    """Read embeddable image bytes keyed by file id.

    Images only: python-pptx cannot place a PDF, and generated code cannot open
    a path, so anything not loaded here simply cannot appear in the deck.

    The path is rebuilt from the trusted uploads root plus the stored basename
    (same guard as GET /files/{id}/content), so a tampered DB value can never
    make this read outside uploads/files/. Best-effort: an unreadable image is
    skipped and the deck still renders without it.
    """
    wanted = [
        f for f in (included_files or [])
        if str(f.get("content_type") or "").startswith("image/")
    ]
    if not wanted:
        return {}

    try:
        rows = await db.execute(
            select(File).where(File.id.in_([str(f["id"]) for f in wanted]))
        )
        by_id = {str(r.id): r for r in rows.scalars().all()}
    except Exception as e:
        logger.warning(f"artifact images: could not load file rows: {e}")
        return {}

    out: Dict[str, bytes] = {}
    for f in wanted:
        row = by_id.get(str(f["id"]))
        if row is None or not row.path:
            continue
        try:
            disk_path = Path.cwd() / "uploads" / "files" / Path(row.path).name
            async with aiofiles.open(disk_path, "rb") as fh:
                out[str(f["id"])] = await fh.read()
        except Exception as e:
            logger.warning(f"artifact images: could not read image {f['id']}: {e}")
    return out


async def build_file_datauris(db, included_files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Read embedded files from disk and return them as data: URIs.

    Used by every headless render (thumbnail, screenshot, PDF export), none of
    which has an auth context to fetch /files/{id}/content the way the live
    viewer does. Without this the <BowFile> components in an artifact render a
    placeholder and the embedded images/PDFs are simply missing from the export.

    Best-effort: a file that can't be read comes back with an empty dataUri.
    """
    out: List[Dict[str, Any]] = []
    if not included_files:
        return out

    try:
        rows = await db.execute(
            select(File).where(File.id.in_([str(f["id"]) for f in included_files]))
        )
        by_id = {str(r.id): r for r in rows.scalars().all()}
    except Exception as e:
        logger.warning(f"artifact files: could not load file rows: {e}")
        by_id = {}

    for f in included_files:
        data_uri = ""
        row = by_id.get(str(f["id"]))
        if row is not None and row.path:
            try:
                disk_path = Path.cwd() / "uploads" / "files" / Path(row.path).name
                async with aiofiles.open(disk_path, "rb") as fh:
                    raw = await fh.read()
                ct = f.get("content_type") or row.content_type or "application/octet-stream"
                data_uri = f"data:{ct};base64,{base64.b64encode(raw).decode('ascii')}"
            except Exception as e:
                logger.warning(f"artifact files: could not read file {f['id']}: {e}")
        out.append({
            "id": f["id"],
            "content_type": f.get("content_type"),
            "filename": f.get("filename"),
            "dataUri": data_uri,
        })
    return out

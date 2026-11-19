"""Which files an artifact may attach: one rule for create_artifact and edit_artifact.

Attaching a file makes it readable by everyone who can view the artifact, and
the ids come from the model, so both tools resolve them the same way: only
files the run's user may see, only images and PDFs (what ``<BowFile>`` is
documented to render), ids compared in one normalised form.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Optional

from sqlalchemy import select

from app.ai.tools.implementations._artifact_refs import normalize_file_id

logger = logging.getLogger(__name__)


class FileLookupFailed(Exception):
    """The lookup itself failed (database or access check), not the files.

    Callers report it as retryable instead of "you cannot access this file",
    which would send the agent away from the user's own file.
    """


def attachable_type(content_type: Optional[str]) -> bool:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    return media_type.startswith("image/") or media_type == "application/pdf"


def file_entry(f: Any) -> Dict[str, Any]:
    """The ``content.files`` entry for a File row."""
    return {"id": str(f.id), "content_type": f.content_type or "application/octet-stream", "filename": f.filename}


async def attachable_files(db, *, user, report, organization, file_ids: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    """The requested files this run may attach, keyed by normalised id.

    A requested id missing from the result does not exist, isn't visible to
    this user, or isn't an image or PDF; callers give one answer for all three
    so nothing is revealed about files the user can't see.

    Raises FileLookupFailed when the lookup itself fails. It runs in a
    savepoint, so a failed statement doesn't leave the run's shared session
    in an aborted transaction (Postgres).
    """
    ids = list(dict.fromkeys(normalize_file_id(f) for f in file_ids or [] if str(f or "").strip()))
    if not ids:
        return {}
    from app.models.file import File
    from app.services.file_access_service import run_viewable_file_ids

    try:
        async with db.begin_nested():
            allowed = await run_viewable_file_ids(db, user=user, report=report, organization=organization, file_ids=ids)
            rows = (await db.execute(select(File).where(File.id.in_(list(allowed))))).scalars().all() if allowed else []
    except Exception as e:
        logger.warning("artifact files: lookup failed for %d id(s): %r", len(ids), e)
        raise FileLookupFailed(str(e)) from e
    return {normalize_file_id(f.id): file_entry(f) for f in rows if attachable_type(f.content_type)}

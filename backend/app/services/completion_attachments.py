"""Files attached to a user message (completion).

A file uploaded with a message is linked to that message's head completion
through ``report_file_association.completion_id``. That table holds one row per
(report, file), so a file can be linked to only one message that way.

Rerun re-sends an earlier prompt with the same attachments. The new head
completion carries their ids in ``prompt.file_ids``. Those ids count as
attachments only while the file is still linked to the completion's report, so
an id from another report (or a removed file) is ignored.

Every reader of "this message's attachments" goes through
``attached_files_by_completion`` so both kinds of link behave the same: the
chat bubble, the agent's vision input, history tags and the files context.
"""
from __future__ import annotations

import logging
from typing import Dict, Iterable, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def prompt_file_ids(prompt) -> List[str]:
    """``prompt.file_ids`` of a stored prompt dict, as clean strings."""
    if not isinstance(prompt, dict):
        return []
    raw = prompt.get("file_ids")
    if not isinstance(raw, list):
        return []
    return [str(i) for i in raw if isinstance(i, (str, int)) and str(i)]


async def attached_files_by_completion(
    db: AsyncSession,
    completion_ids: Iterable[str],
    *,
    images_only: bool = False,
) -> Dict[str, list]:
    """Map each completion id to the File rows attached to it.

    Includes files linked through ``report_file_association.completion_id`` and
    files named in the completion's ``prompt.file_ids`` that are linked to the
    same report. Each file appears once per completion. Completions with no
    attachments are absent from the result.
    """
    from app.models.completion import Completion
    from app.models.file import File
    from app.models.report_file_association import report_file_association

    ids = [str(c) for c in dict.fromkeys(completion_ids or []) if c]
    if not ids:
        return {}

    out: Dict[str, list] = {}
    seen: Dict[str, set] = {}

    def _add(cid: str, f) -> None:
        bucket = seen.setdefault(cid, set())
        fid = str(f.id)
        if fid in bucket:
            return
        bucket.add(fid)
        out.setdefault(cid, []).append(f)

    def _image_filter(stmt):
        return stmt.where(File.content_type.like("image/%")) if images_only else stmt

    linked = await db.execute(_image_filter(
        select(report_file_association.c.completion_id, File)
        .join(File, File.id == report_file_association.c.file_id)
        .where(report_file_association.c.completion_id.in_(ids))
    ))
    for cid, f in linked.all():
        if cid:
            _add(str(cid), f)

    # Rerun links: ids stored on the prompt, restricted to the report's files.
    prompts = await db.execute(
        select(Completion.id, Completion.report_id, Completion.prompt)
        .where(Completion.id.in_(ids), Completion.role == "user")
    )
    wanted: Dict[str, List[str]] = {}
    report_of: Dict[str, str] = {}
    for cid, report_id, prompt in prompts.all():
        fids = prompt_file_ids(prompt)
        if fids and report_id:
            wanted[str(cid)] = fids
            report_of[str(cid)] = str(report_id)
    if not wanted:
        return out

    all_fids = {fid for fids in wanted.values() for fid in fids}
    report_ids = set(report_of.values())
    rows = await db.execute(_image_filter(
        select(report_file_association.c.report_id, File)
        .join(File, File.id == report_file_association.c.file_id)
        .where(
            report_file_association.c.report_id.in_(report_ids),
            report_file_association.c.file_id.in_(all_fids),
        )
    ))
    by_report: Dict[str, Dict[str, object]] = {}
    for report_id, f in rows.all():
        by_report.setdefault(str(report_id), {})[str(f.id)] = f
    for cid, fids in wanted.items():
        files = by_report.get(report_of[cid], {})
        for fid in fids:
            f = files.get(fid)
            if f is not None:
                _add(cid, f)
    return out

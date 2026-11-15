"""File retention: delete files older than the org's ``file_retention_days``.

Off by default (0 keeps files forever). When set, a nightly job deletes files
created more than that many days ago, the same way a user's own delete does
(``file_service.purge_files``): every link to the file, its tags and schemas,
the row, then the bytes.

Some files are never expired, however old:

* files a dashboard owns (a resource binding, a published release, or an
  ``artifact_resource`` file) — the dashboard would break;
* files in an agent's or a project's library — they are shared knowledge,
  managed on the agent or project, not leftovers of a run;
* uploads attached to a report that runs on a schedule — the next run still
  needs them. The tool output those runs leave behind does expire: that is
  the pile-up retention exists for.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.artifact_release import artifact_release_file_association
from app.models.artifact_resource import ArtifactFileBinding
from app.models.data_source_file_association import data_source_file_association
from app.models.file import File, report_file_association
from app.models.organization import Organization
from app.models.organization_settings import OrganizationSettings
from app.models.project import project_file_association
from app.models.report import Report
from app.models.scheduled_prompt import ScheduledPrompt

logger = logging.getLogger(__name__)

SETTING_KEY = "file_retention_days"
MAX_RETENTION_DAYS = 3650
# Files deleted per transaction; the job loops until nothing is left.
BATCH_SIZE = 500


def retention_days_from_config(config: Optional[dict]) -> int:
    """The org's retention in days, or 0 (keep forever) when unset/invalid."""
    raw = (config or {}).get(SETTING_KEY)
    value = raw.get("value") if isinstance(raw, dict) else raw
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return 0
    return min(value, MAX_RETENTION_DAYS)


def _expirable_files(organization_id: str, cutoff: datetime):
    """Files of the org created before ``cutoff`` that retention may delete."""
    scheduled_report = or_(
        Report.cron_schedule.isnot(None),
        exists().where(ScheduledPrompt.report_id == Report.id, ScheduledPrompt.is_active.is_(True)),
    )
    in_scheduled_report = exists().where(
        report_file_association.c.file_id == File.id,
        report_file_association.c.report_id == Report.id,
        scheduled_report,
    )
    return (
        select(File)
        .where(
            File.organization_id == str(organization_id),
            File.created_at < cutoff,
            or_(File.source_kind.is_(None), File.source_kind != "artifact_resource"),
            ~exists().where(ArtifactFileBinding.file_id == File.id),
            ~exists().where(artifact_release_file_association.c.file_id == File.id),
            ~exists().where(data_source_file_association.c.file_id == File.id),
            ~exists().where(project_file_association.c.file_id == File.id),
            # Uploads feeding a scheduled report stay; its tool output expires.
            ~(or_(File.source_kind.is_(None), File.source_kind == "upload") & in_scheduled_report),
        )
        .order_by(File.created_at)
    )


async def purge_expired_files_for_org(
    db: AsyncSession, organization_id: str, retention_days: int, now: Optional[datetime] = None
) -> int:
    """Delete the org's files older than ``retention_days``. Returns how many."""
    from app.services.file_service import purge_files

    if retention_days <= 0:
        return 0
    cutoff = (now or datetime.utcnow()) - timedelta(days=retention_days)
    total = 0
    while True:
        batch = (await db.execute(_expirable_files(organization_id, cutoff).limit(BATCH_SIZE))).scalars().all()
        if not batch:
            break
        total += len(await purge_files(db, list(batch)))
        if len(batch) < BATCH_SIZE:
            break
    if total:
        logger.info(
            "File retention: deleted %d files older than %d days for org %s",
            total, retention_days, organization_id,
        )
    return total


async def apply_file_retention(now: Optional[datetime] = None) -> int:
    """Apply every org's file retention once. Orgs with it off are skipped."""
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(Organization.id, OrganizationSettings.config)
            .join(OrganizationSettings, Organization.id == OrganizationSettings.organization_id)
            .where(Organization.deleted_at.is_(None))
        )).all()

    total = 0
    for org_id, config in rows:
        days = retention_days_from_config(config)
        if not days:
            continue
        try:
            async with async_session_maker() as db:
                total += await purge_expired_files_for_org(db, str(org_id), days, now=now)
        except Exception:  # noqa: BLE001 — one org's failure must not stop the others
            logger.exception("File retention failed for org %s", org_id)
    return total


async def purge_expired_files() -> int:
    """The nightly job. It fires in every worker (shared job store), so it
    claims the run first: only one worker purges."""
    from app.core.scheduler import claim_scheduled_run

    if not await asyncio.to_thread(claim_scheduled_run, "purge_expired_files_daily"):
        return 0
    return await apply_file_retention()

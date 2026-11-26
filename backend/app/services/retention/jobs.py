"""The nightly retention run: reports, then artifacts, then file bytes a
delete could not remove yet. (File retention keeps its own 03:45 job in
``file_retention_service``.)

Each org is done in its own sessions, so one org's failure never stops the
others, and each deletion is written to the org's audit log with counts —
never titles or content, which are what was deleted.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select

logger = logging.getLogger(__name__)

JOB_ID = "purge_retention_daily"


async def _audit(organization_id: str, action: str, details: dict) -> None:
    from app.dependencies import async_session_maker
    from app.ee.audit.service import audit_service

    try:
        async with async_session_maker() as db:
            await audit_service.log(db=db, organization_id=str(organization_id), action=action,
                                    resource_type="retention", details=details)
    except Exception:  # noqa: BLE001 — the deletion already happened
        logger.exception("Retention: could not write the audit entry for org %s", organization_id)


async def apply_retention(now: Optional[datetime] = None) -> dict:
    """Apply every org's report and artifact retention once. Orgs with both
    off are skipped. Returns totals."""
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.organization_settings import OrganizationSettings
    from app.services.retention.artifacts import purge_expired_artifacts_for_org
    from app.services.retention.blobs import retry_pending
    from app.services.retention.reports import purge_expired_reports_for_org
    from app.services.retention.settings import ARTIFACT_RETENTION_KEY, REPORT_RETENTION_KEY, days_from_config

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(Organization.id, OrganizationSettings.config)
            .join(OrganizationSettings, Organization.id == OrganizationSettings.organization_id)
            .where(Organization.deleted_at.is_(None))
        )).all()

    totals = {"reports": 0, "artifacts": 0, "files": 0}
    for org_id, config in rows:
        report_days = days_from_config(config, REPORT_RETENTION_KEY)
        artifact_days = days_from_config(config, ARTIFACT_RETENTION_KEY)
        if report_days:
            try:
                async with async_session_maker() as db:
                    result = await purge_expired_reports_for_org(db, str(org_id), report_days, now=now)
                if result["reports"]:
                    totals["reports"] += result["reports"]
                    totals["files"] += result["tables"].get("files", 0)
                    logger.info("Report retention: deleted %d reports (%d days) for org %s",
                                result["reports"], report_days, org_id)
                    await _audit(str(org_id), "retention.reports_deleted",
                                 {"retention_days": report_days, **result})
            except Exception:  # noqa: BLE001 — one org's failure must not stop the others
                logger.exception("Report retention failed for org %s", org_id)
        if artifact_days:
            try:
                async with async_session_maker() as db:
                    result = await purge_expired_artifacts_for_org(db, str(org_id), artifact_days, now=now)
                if result["artifacts"]:
                    totals["artifacts"] += result["artifacts"]
                    totals["files"] += result["tables"].get("files", 0)
                    logger.info("Artifact retention: deleted %d artifacts (%d days) for org %s",
                                result["artifacts"], artifact_days, org_id)
                    await _audit(str(org_id), "retention.artifacts_deleted",
                                 {"retention_days": artifact_days, **result})
            except Exception:  # noqa: BLE001
                logger.exception("Artifact retention failed for org %s", org_id)

    try:
        totals["blobs"] = await retry_pending()
    except Exception:  # noqa: BLE001
        logger.exception("Retention: retrying pending file byte deletes failed")
    return totals


async def purge_expired() -> dict:
    """The nightly job. It fires in every worker (shared job store), so it
    claims the run first: only one worker deletes."""
    from app.core.scheduler import claim_scheduled_run

    if not await asyncio.to_thread(claim_scheduled_run, JOB_ID):
        return {}
    return await apply_retention()


async def preview(db, organization_id: str, config: Optional[dict], now: Optional[datetime] = None) -> dict:
    """What tonight's run would delete for this org, with its current settings."""
    from datetime import timedelta

    from app.services.file_retention_service import _expirable_files
    from app.services.retention import artifacts, reports
    from app.services.retention.settings import (
        ARTIFACT_RETENTION_KEY, FILE_RETENTION_KEY, REPORT_RETENTION_KEY, days_from_config,
    )
    from sqlalchemy import func

    r_days = days_from_config(config, REPORT_RETENTION_KEY)
    a_days = days_from_config(config, ARTIFACT_RETENTION_KEY)
    f_days = days_from_config(config, FILE_RETENTION_KEY)
    files = 0
    if f_days:
        cutoff = (now or datetime.utcnow()) - timedelta(days=f_days)
        sub = _expirable_files(organization_id, cutoff).order_by(None).subquery()
        files = int((await db.execute(select(func.count()).select_from(sub))).scalar() or 0)
    return {
        "report_retention_days": r_days or None,
        "artifact_retention_days": a_days or None,
        "file_retention_days": f_days or None,
        "reports": await reports.preview_for_org(db, organization_id, r_days, now=now),
        "artifacts": await artifacts.preview_for_org(db, organization_id, a_days, now=now),
        "files": files,
    }

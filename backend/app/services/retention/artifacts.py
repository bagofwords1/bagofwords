"""Artifact retention: delete, for good, dashboards untouched for N days.

Off by default (``artifact_retention_days`` empty). When set, the nightly
job deletes every artifact (dashboard or page) of the org that nobody has
edited, run or published inside the window, with its versions, releases
(and the queries, steps and visualizations a release owns), resources,
records, file bindings, shares, drafts, views and activity — and the files
nothing else uses. The conversations it came from are not deleted (that is
report retention); forks of it keep their own copy.

Kept, however old: a published dashboard, one shared in any mode or with a
user or group, one on a schedule, one embedded in a host application, and
one whose conversation was active inside the window.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.artifact import Artifact, ArtifactVersion
from app.models.artifact_release import ArtifactConversation, ArtifactShare
from app.models.host_application import ArtifactHostApplication
from app.models.report import Report
from app.services.retention import purge

BATCH_SIZE = 100


def _kept_whatever_age():
    """An artifact that must stay however old."""
    return or_(
        Artifact.published_release_id.isnot(None),
        and_(Artifact.visibility.isnot(None), Artifact.visibility != "none"),
        Artifact.cron_schedule.isnot(None),
        exists().where(ArtifactShare.artifact_id == Artifact.id, ArtifactShare.deleted_at.is_(None)),
        exists().where(ArtifactHostApplication.artifact_id == Artifact.id,
                       ArtifactHostApplication.deleted_at.is_(None)),
    )


def _touched_since(cutoff: datetime):
    """An artifact someone edited, ran or worked on inside the window."""
    # Each comparison is guarded: this is negated, and NOT (NULL) would drop
    # a never-run artifact from the candidates instead of keeping it in.
    def since(col):
        return and_(col.isnot(None), col >= cutoff)

    return or_(
        since(Artifact.created_at),
        since(Artifact.updated_at),
        since(Artifact.content_updated_at),
        since(Artifact.last_run_at),
        exists().where(ArtifactVersion.artifact_id == Artifact.id, ArtifactVersion.created_at >= cutoff),
        exists().where(Report.id == Artifact.report_id,
                       func.coalesce(Report.last_activity_at, Report.created_at) >= cutoff),
        exists().where(ArtifactConversation.artifact_id == Artifact.id,
                       ArtifactConversation.report_id == Report.id,
                       func.coalesce(Report.last_activity_at, Report.created_at) >= cutoff),
    )


async def protected_artifact_ids(db: AsyncSession, organization_id: str, cutoff: datetime) -> set[str]:
    """Artifacts no retention may delete tonight (report retention checks
    its purge tree against this)."""
    rows = await db.execute(
        select(Artifact.id).where(
            Artifact.organization_id == str(organization_id),
            or_(_kept_whatever_age(), _touched_since(cutoff)),
        )
    )
    return {str(r) for (r,) in rows.all()}


def expirable_artifacts(organization_id: str, cutoff: datetime):
    return (
        select(Artifact.id)
        .where(
            Artifact.organization_id == str(organization_id),
            ~_kept_whatever_age(),
            ~_touched_since(cutoff),
        )
        .order_by(Artifact.created_at)
    )


async def purge_artifacts(db: AsyncSession, artifact_ids: list[str]) -> dict:
    """Delete ``artifact_ids`` and everything they own for good, then the
    files nothing links to any more. Commits. Returns per-table counts."""
    from app.services.retention.blobs import purge_file_ids

    if not artifact_ids:
        return {}
    plan = await purge.closure(db, {"artifacts": artifact_ids})
    files = await purge.linked_file_ids(db, plan)
    counts = await purge.execute(db, plan)
    await db.commit()
    orphan_files = await purge.unreferenced_files(db, files)
    if orphan_files:
        counts["files"] = await purge_file_ids(db, orphan_files)
    return counts


async def purge_expired_artifacts_for_org(
    db: AsyncSession, organization_id: str, retention_days: int, now: Optional[datetime] = None,
) -> dict:
    if retention_days <= 0:
        return {"artifacts": 0, "tables": {}}
    cutoff = (now or datetime.utcnow()) - timedelta(days=retention_days)
    totals: dict[str, int] = {}
    done = 0
    while True:
        batch = [str(r) for (r,) in (await db.execute(
            expirable_artifacts(organization_id, cutoff).limit(BATCH_SIZE))).all()]
        if not batch:
            break
        for table, n in (await purge_artifacts(db, batch)).items():
            totals[table] = totals.get(table, 0) + n
        done += len(batch)
        if len(batch) < BATCH_SIZE:
            break
    return {"artifacts": done, "tables": totals}


async def preview_for_org(db: AsyncSession, organization_id: str, retention_days: int,
                          now: Optional[datetime] = None) -> int:
    if retention_days <= 0:
        return 0
    cutoff = (now or datetime.utcnow()) - timedelta(days=retention_days)
    sub = expirable_artifacts(organization_id, cutoff).order_by(None).subquery()
    return int((await db.execute(select(func.count()).select_from(sub))).scalar() or 0)

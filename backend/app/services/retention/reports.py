"""Report retention: delete, for good, conversations untouched for N days.

Off by default (``report_retention_days`` empty). When set, the nightly job
deletes every report of the org whose last activity is older than the
window, with everything it owns (see ``purge.POLICY``): completions, agent
executions, plans, plan decisions, tool executions, completion blocks,
context snapshots, widgets, steps, queries, sub-agents, notes, mentions,
check-ins, its never-published dashboards, and the files nothing else uses.

Kept, however old:

* a report that still works on its own: on a schedule (``cron_schedule`` or
  an active scheduled prompt), or fed by an active webhook;
* a report someone was given: shared or published in any mode, or shared
  with a user or group;
* eval test reports (their results need the run);
* a report with a run in progress or a completion inside the window;
* a report whose deletion would take a dashboard that must stay — published,
  shared, scheduled, embedded, or edited inside the window.

Usage records, instructions and eval cases learned from a deleted report
stay, with the link cleared.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_execution import AgentExecution
from app.models.artifact import Artifact
from app.models.completion import Completion
from app.models.report import Report
from app.models.report_share import ReportShare
from app.models.scheduled_prompt import ScheduledPrompt
from app.models.webhook import Webhook
from app.services.retention import purge
from app.services.retention.artifacts import protected_artifact_ids

BATCH_SIZE = 100


def expirable_reports(organization_id: str, cutoff: datetime):
    """Reports of the org that report retention may delete (before the
    dashboard check, which needs the purge tree)."""
    activity = func.coalesce(Report.last_activity_at, Report.created_at)
    return (
        select(Report.id)
        .where(
            Report.organization_id == str(organization_id),
            Report.report_type != "test",
            activity < cutoff,
            or_(Report.created_at.is_(None), Report.created_at < cutoff),
            Report.cron_schedule.is_(None),
            or_(Report.status.is_(None), Report.status != "published"),
            or_(Report.artifact_visibility.is_(None), Report.artifact_visibility == "none"),
            or_(Report.conversation_visibility.is_(None), Report.conversation_visibility == "none"),
            Report.conversation_share_enabled.isnot(True),
            ~exists().where(ScheduledPrompt.report_id == Report.id, ScheduledPrompt.deleted_at.is_(None),
                            ScheduledPrompt.is_active.is_(True)),
            ~exists().where(Webhook.report_id == Report.id, Webhook.deleted_at.is_(None),
                            Webhook.is_active.is_(True)),
            ~exists().where(ReportShare.report_id == Report.id, ReportShare.deleted_at.is_(None)),
            ~exists().where(AgentExecution.report_id == Report.id, AgentExecution.status == "in_progress"),
            ~exists().where(Completion.report_id == Report.id, Completion.created_at >= cutoff),
        )
        .order_by(activity)
        .execution_options(include_rewound=True)
    )


async def _safe_roots(db: AsyncSession, organization_id: str, report_ids: list[str],
                      cutoff: datetime) -> tuple[list[str], purge.Closure]:
    """The reports of ``report_ids`` whose purge takes no dashboard that must
    stay, and the purge tree of those."""
    protected = await protected_artifact_ids(db, organization_id, cutoff)
    plan = await purge.closure(db, {"reports": report_ids})
    if not (plan.ids.get("artifacts", set()) & protected
            or await _touches_protected_versions(db, plan, protected)):
        return report_ids, plan
    # Some report would take a protected dashboard: find which, one by one.
    keep: list[str] = []
    for rid in report_ids:
        one = await purge.closure(db, {"reports": [rid]})
        if one.ids.get("artifacts", set()) & protected or await _touches_protected_versions(db, one, protected):
            continue
        keep.append(rid)
    if not keep:
        return [], purge.Closure()
    return keep, await purge.closure(db, {"reports": keep})


async def _touches_protected_versions(db: AsyncSession, plan: purge.Closure, protected: set[str]) -> bool:
    """Would the purge delete a version or release of a protected dashboard?"""
    from app.models.artifact import ArtifactVersion
    from app.models.artifact_release import ArtifactRelease

    if not protected:
        return False
    for model, key in ((ArtifactVersion, "artifact_versions"), (ArtifactRelease, "artifact_releases")):
        ids = list(plan.ids.get(key, set()))
        for chunk in purge._chunks(ids):
            hit = (await db.execute(select(exists().where(
                model.id.in_(chunk), model.artifact_id.in_(list(protected)))))).scalar()
            if hit:
                return True
    return False


async def purge_reports(db: AsyncSession, report_ids: list[str], plan: Optional[purge.Closure] = None) -> dict:
    """Delete ``report_ids`` and their whole tree for good, then the files
    nothing links to any more. Commits. Returns per-table counts."""
    from app.services.retention.blobs import purge_file_ids

    if not report_ids:
        return {}
    plan = plan or await purge.closure(db, {"reports": report_ids})
    files = await purge.linked_file_ids(db, plan)
    counts = await purge.execute(db, plan)
    await db.commit()
    orphan_files = await purge.unreferenced_files(db, files)
    if orphan_files:
        counts["files"] = await purge_file_ids(db, orphan_files)
    return counts


async def purge_expired_reports_for_org(
    db: AsyncSession, organization_id: str, retention_days: int, now: Optional[datetime] = None,
) -> dict:
    """Delete the org's reports untouched for ``retention_days``. Returns
    ``{"reports": n, "tables": {...}}``."""
    if retention_days <= 0:
        return {"reports": 0, "tables": {}}
    cutoff = (now or datetime.utcnow()) - timedelta(days=retention_days)
    totals: dict[str, int] = {}
    done: set[str] = set()
    skipped: set[str] = set()
    while True:
        batch = [str(r) for (r,) in (await db.execute(
            expirable_reports(organization_id, cutoff).limit(BATCH_SIZE + len(skipped))
        )).all() if str(r) not in skipped and str(r) not in done][:BATCH_SIZE]
        if not batch:
            break
        roots, plan = await _safe_roots(db, organization_id, batch, cutoff)
        skipped |= set(batch) - set(roots)
        if roots:
            for table, n in (await purge_reports(db, roots, plan)).items():
                totals[table] = totals.get(table, 0) + n
            done |= set(roots)
        if len(batch) < BATCH_SIZE:
            break
    return {"reports": len(done), "kept_for_dashboards": len(skipped), "tables": totals}


async def preview_for_org(db: AsyncSession, organization_id: str, retention_days: int,
                          now: Optional[datetime] = None) -> int:
    """How many reports tonight's run would delete: the same selection and
    dashboard check as the run, without deleting anything."""
    if retention_days <= 0:
        return 0
    cutoff = (now or datetime.utcnow()) - timedelta(days=retention_days)
    ids = [str(r) for (r,) in (await db.execute(expirable_reports(organization_id, cutoff))).all()]
    total = 0
    for i in range(0, len(ids), BATCH_SIZE):
        roots, _ = await _safe_roots(db, organization_id, ids[i:i + BATCH_SIZE], cutoff)
        total += len(roots)
    return total

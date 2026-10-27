"""Conversations and their dashboards after the data migration (phase 9).

The migration records on a dashboard the conversation whose /r link it took
over (artifacts.legacy_report_id) and leaves the conversation's old sharing
and schedule columns as they were (decision 14). From then on those columns
decide nothing for that conversation's dashboards (decisions 13 and 15):

- the old /r and artifact paths stop admitting readers by the conversation's
  old sharing -- the /r page forwards to /d, where the dashboard's own
  sharing decides; the conversation's editors keep their access;
- the conversations list reads its badge, schedule icon, filters and "shared
  with me" from the conversation's dashboards.

A conversation without a dashboard (widget era) keeps being served by its
own columns, as before.
"""
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import aliased

from app.models.artifact import Artifact
from app.models.artifact_release import ArtifactShare
from app.models.report import Report

OPEN_TO_MEMBERS = ("public", "internal")
_RANK = {"public": 3, "internal": 2, "shared": 1}


def link_taken_over(report_id_column=Report.id):
    """SQL: a dashboard took over this conversation's old link (deleted
    since or not: a deleted dashboard does not hand the link back)."""
    holder = aliased(Artifact)
    return exists().where(holder.legacy_report_id == report_id_column)


async def is_link_taken_over(db, report_id) -> bool:
    return (await db.execute(
        select(Artifact.id).where(Artifact.legacy_report_id == str(report_id)).limit(1)
    )).first() is not None


def _live_dashboard(*conditions):
    """SQL: the conversation has a live dashboard meeting the conditions
    (each a function of the dashboard)."""
    dash = aliased(Artifact)
    return exists().where(dash.report_id == Report.id, dash.deleted_at.is_(None),
                          *(condition(dash) for condition in conditions))


def has_dashboards():
    return _live_dashboard()


def speaks_for_itself():
    """SQL: the conversation's own sharing and schedule columns count -- it
    has no live dashboard, and none ever took its link over (once one did,
    the columns are only the migration's backup, deleted dashboard or not)."""
    return ~or_(has_dashboards(), link_taken_over())


def has_shared_dashboard():
    """A published dashboard someone besides its editors can open."""
    return _live_dashboard(lambda d: d.published_release_id.isnot(None), lambda d: d.visibility != "none")


def has_scheduled_dashboard():
    return _live_dashboard(lambda d: d.cron_schedule.isnot(None), lambda d: d.cron_schedule != "")


def counts_as_shared():
    """SQL: the conversation reads as shared ("published") -- by a shared
    dashboard, or by its own old status where it has no dashboard."""
    return or_(and_(speaks_for_itself(), Report.status == "published"), has_shared_dashboard())


async def shared_ids(db, report_ids) -> set[str]:
    if not report_ids:
        return set()
    return {str(r) for (r,) in (await db.execute(
        select(Report.id).where(Report.id.in_([str(i) for i in report_ids]), counts_as_shared()))).all()}


def dashboard_visible_to(user_id, group_ids_subquery):
    """SQL: one of the conversation's published dashboards is open to the
    user -- to the organization, or shared with them or a group of theirs."""
    def open_to_user(dash):
        granted = exists().where(
            ArtifactShare.artifact_id == dash.id,
            or_(ArtifactShare.user_id == str(user_id), ArtifactShare.group_id.in_(group_ids_subquery)))
        return or_(dash.visibility.in_(OPEN_TO_MEMBERS), and_(dash.visibility == "shared", granted))

    return _live_dashboard(lambda d: d.published_release_id.isnot(None), open_to_user)


async def list_fields(db, report_ids) -> dict[str, dict]:
    """Per conversation with dashboards: the most open visibility among its
    published dashboards, and a dashboard schedule if one has it."""
    out: dict[str, dict] = {}
    if not report_ids:
        return out
    # Taken over, its dashboard deleted since: nothing shared or scheduled.
    for (rid,) in (await db.execute(select(Artifact.legacy_report_id).where(
            Artifact.legacy_report_id.in_([str(r) for r in report_ids])))).all():
        out[str(rid)] = {"artifact_visibility": "none", "cron_schedule": None}
    rows = (await db.execute(
        select(Artifact.report_id, Artifact.visibility, Artifact.published_release_id, Artifact.cron_schedule)
        .where(Artifact.report_id.in_([str(r) for r in report_ids]), Artifact.deleted_at.is_(None))
        .order_by(Artifact.created_at.desc())
    )).all()
    for rid, visibility, release_id, cron in rows:
        entry = out.setdefault(str(rid), {"artifact_visibility": "none", "cron_schedule": None})
        if release_id and _RANK.get(visibility, 0) > _RANK.get(entry["artifact_visibility"], 0):
            entry["artifact_visibility"] = visibility
        if cron and not entry["cron_schedule"]:
            entry["cron_schedule"] = cron
    return out


async def published_pictures(db, report_ids, user) -> dict[str, str | None]:
    """Per conversation with a published dashboard: the picture this reader
    may see -- a release's, of a dashboard they can open (newest first; None
    when none has one). Never the draft's newer picture, nor the picture of
    a dashboard shared with someone else."""
    from fastapi import HTTPException

    from app.errors import AppError
    from app.models.artifact_release import ArtifactRelease
    from app.services import dashboard_access

    out: dict[str, str | None] = {}
    if not report_ids:
        return out
    rows = (await db.execute(
        select(Artifact, ArtifactRelease.thumbnail_path)
        .join(ArtifactRelease, ArtifactRelease.id == Artifact.published_release_id)
        .where(Artifact.report_id.in_([str(r) for r in report_ids]), Artifact.deleted_at.is_(None))
        .order_by(Artifact.created_at.desc())
    )).all()
    for artifact, path in rows:
        rid = str(artifact.report_id)
        out.setdefault(rid, None)
        if out[rid] is not None or not path:
            continue
        try:
            await dashboard_access.admit_reader(db, artifact, user)
        except (AppError, HTTPException):
            continue
        out[rid] = path
    return out


async def link_target(db, report_id, user=None) -> str | None:
    """Where /r/{report_id} leads: the dashboard the data migration recorded
    for it (frozen: what the link showed on upgrade day). A conversation made
    since has none recorded; its link leads as /r chose (decision 2): the
    newest published page, then a published doc, then the newest unpublished
    one (/d sends its editors to the draft) -- the first of those the reader
    (signed in or not) can open. None when they can open none: /r serves the
    conversation as before rather than leading to a /d that refuses them."""
    from fastapi import HTTPException

    from app.errors import AppError
    from app.services import dashboard_access

    recorded = (await db.execute(
        select(Artifact.id).where(Artifact.legacy_report_id == str(report_id), Artifact.deleted_at.is_(None))
    )).scalar_one_or_none()
    if recorded is not None:
        return str(recorded)
    if await is_link_taken_over(db, report_id):
        return None  # its dashboard was deleted: the link does not move to another
    candidates = (await db.execute(
        select(Artifact).where(Artifact.report_id == str(report_id), Artifact.deleted_at.is_(None),
                               Artifact.owner_id.isnot(None))
    )).scalars().all()
    candidates.sort(key=lambda a: (a.published_release_id is None, a.mode == "doc",
                                   -(a.created_at.timestamp() if a.created_at else 0)))
    for artifact in candidates:
        try:
            await dashboard_access.admit_reader(db, artifact, user)
            return str(artifact.id)
        except (AppError, HTTPException):
            continue
    return None

"""Per-artifact sharing: who may open which artifact of a report on /r/{id}.

Every artifact (dashboard / deck / doc) carries its own visibility
('none' | 'shared' | 'internal' | 'public') and, for 'shared', its own grants
in artifact_shares. The rules per artifact are the ones the report-level
check has always applied, just evaluated per artifact:

- the report's bow_source_access read gate runs first;
- anyone who can view the report's project sees every artifact in it
  (a project is a sharing boundary);
- the report owner sees every artifact;
- 'public' opens to anyone, 'internal' to members of the report's org,
  'shared' to granted users and members of granted groups, 'none' to nobody
  else.

A report-level surface (the report metadata, chat, queries) is open when the
caller may open at least one of the report's artifacts. Reports without any
live artifact keep the report-level fields (reports.artifact_visibility +
report_shares) — there is nothing to share per artifact yet.

The report-level fields are kept as an aggregate of the artifacts (most open
visibility, union of grants) by sync_report_aggregate, so readers that only
ask "is anything in this report shared" (listings, retention, schedulers,
creator-mode runs) keep working unchanged.

Conversation sharing (conversation_visibility, report_shares with
share_type='conversation') is a separate surface and is not handled here.
"""

from __future__ import annotations

from typing import Iterable, Optional

from fastapi import HTTPException
from sqlalchemy import delete, or_, select

from app.models.artifact import Artifact
from app.models.artifact_share import ArtifactShare
from app.models.report import Report

VISIBILITY_ORDER = ("none", "shared", "internal", "public")


def most_open(visibilities: Iterable[str]) -> str:
    rank = {v: i for i, v in enumerate(VISIBILITY_ORDER)}
    return max(visibilities, key=lambda v: rank.get(v or "none", 0), default="none")


async def project_grants_view(db, report: Report, user) -> bool:
    """True when the caller can view the project the report sits in."""
    if user is None or not getattr(report, "project_id", None):
        return False
    from app.models.project import Project
    from app.services.project_service import project_service
    proj = (await db.execute(
        select(Project).where(
            Project.id == report.project_id,
            Project.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    return proj is not None and await project_service.user_can_view_project(db, user, proj)


async def _live_artifacts(db, report_id, artifact_ids: Optional[Iterable[str]] = None) -> list[tuple[str, str]]:
    stmt = select(Artifact.id, Artifact.visibility).where(
        Artifact.report_id == str(report_id),
        Artifact.deleted_at.is_(None),
    )
    if artifact_ids is not None:
        stmt = stmt.where(Artifact.id.in_([str(a) for a in artifact_ids]))
    rows = (await db.execute(stmt)).all()
    return [(str(aid), vis or "none") for aid, vis in rows]


async def report_has_live_artifacts(db, report_id) -> bool:
    row = (await db.execute(
        select(Artifact.id).where(
            Artifact.report_id == str(report_id),
            Artifact.deleted_at.is_(None),
        ).limit(1)
    )).first()
    return row is not None


async def _is_org_member(db, user, organization_id) -> bool:
    from app.models.membership import Membership
    row = (await db.execute(
        select(Membership.id).where(
            Membership.user_id == user.id,
            Membership.organization_id == organization_id,
        ).limit(1)
    )).first()
    return row is not None


async def _decide(db, report: Report, user, artifacts: list[tuple[str, str]]) -> tuple[set[str], int]:
    """Split artifacts into the ones the caller's grants open and, for the
    rest, the status a denial should carry (401 login needed, 403 denied,
    404 private). Owner/project/bow are the caller's job."""
    allowed: set[str] = set()
    needs_login = False
    denied = False

    public = [a for a, v in artifacts if v == "public"]
    internal = [a for a, v in artifacts if v == "internal"]
    shared = [a for a, v in artifacts if v == "shared"]
    allowed.update(public)

    if internal:
        if user is None:
            needs_login = True
        elif await _is_org_member(db, user, report.organization_id):
            allowed.update(internal)
        else:
            denied = True

    if shared:
        if user is None:
            needs_login = True
        else:
            from app.services.report_service import ReportService
            user_group_ids = ReportService._user_group_ids_subquery(user.id, report.organization_id)
            granted = (await db.execute(
                select(ArtifactShare.artifact_id).where(
                    ArtifactShare.artifact_id.in_(shared),
                    ArtifactShare.deleted_at.is_(None),
                    or_(
                        ArtifactShare.user_id == user.id,
                        ArtifactShare.group_id.in_(user_group_ids),
                    ),
                )
            )).all()
            granted_ids = {str(r[0]) for r in granted}
            allowed.update(granted_ids)
            if len(granted_ids) < len(shared):
                denied = True

    if needs_login:
        status = 401
    elif denied:
        status = 403
    else:
        status = 404
    return allowed, status


def _raise(status: int) -> None:
    if status == 401:
        raise HTTPException(status_code=401, detail="Authentication required")
    if status == 403:
        raise HTTPException(status_code=403, detail="Access denied")
    raise HTTPException(status_code=404, detail="Not found")


def _is_owner(report: Report, user) -> bool:
    return user is not None and str(user.id) == str(report.user_id)


async def is_full_admin(db, report: Report, user) -> bool:
    if user is None:
        return False
    from app.core.permission_resolver import FULL_ADMIN, resolve_permissions
    resolved = await resolve_permissions(db, str(user.id), str(report.organization_id))
    return FULL_ADMIN in resolved.org_permissions


async def visible_artifact_ids(db, report: Report, user, *, admin_sees_all: bool = False) -> Optional[set[str]]:
    """Parent ids of the report's live artifacts the caller may open.

    None means "every artifact" (owner, project viewer, and — on the
    authenticated /api surface, which lets full admins view anything — org
    admins). Does not run the bow_source_access gate; callers have already.
    """
    if _is_owner(report, user):
        return None
    if await project_grants_view(db, report, user):
        return None
    if admin_sees_all and await is_full_admin(db, report, user):
        return None
    allowed, _ = await _decide(db, report, user, await _live_artifacts(db, report.id))
    return allowed


async def query_ids_for_contents(db, contents: Iterable[Optional[dict]]) -> list[str]:
    """The queries behind artifact versions: each version's content lists the
    visualizations it shows (visualization_ids), each visualization reads one
    query. Ordered by first appearance, no duplicates."""
    from app.models.visualization import Visualization

    viz_ids = list(dict.fromkeys(
        str(v) for content in contents
        for v in ((content or {}).get("visualization_ids") or []) if v
    ))
    if not viz_ids:
        return []
    rows = (await db.execute(
        select(Visualization.id, Visualization.query_id).where(
            Visualization.id.in_(viz_ids),
            Visualization.deleted_at.is_(None),
        )
    )).all()
    by_viz = {str(vid): str(qid) for vid, qid in rows if qid}
    return list(dict.fromkeys(by_viz[v] for v in viz_ids if v in by_viz))


async def _has_conversation_access(db, report: Report, user) -> bool:
    """True when the caller may read the report's whole conversation — every
    query result is in it, so dashboard scoping has nothing left to hide."""
    from app.services.report_service import ReportService
    try:
        await ReportService()._check_visibility(db, report, "conversation_visibility", user)
    except HTTPException:
        return False
    return True


async def has_full_access(db, report: Report, user) -> bool:
    """True when the caller sees the whole report, every dashboard and the
    conversation behind them: owner, project viewer, or conversation access.
    Report-wide content (agent notes, every query) needs this; opening one
    dashboard is not enough."""
    if _is_owner(report, user) or await project_grants_view(db, report, user):
        return True
    return await _has_conversation_access(db, report, user)


async def visible_query_ids(db, report: Report, user) -> Optional[set[str]]:
    """Ids of the report's queries the caller may read on /r: the ones behind
    the dashboards they may open (any live version — the viewer can switch to
    an older one), plus the queries those queries' filters draw their options
    from. None means every query (owner, project viewer, conversation access,
    or a report without live artifacts, where the report-level gate rules).
    """
    from app.models.artifact import ArtifactVersion
    from app.models.query import Query

    if not await report_has_live_artifacts(db, report.id):
        return None
    if await has_full_access(db, report, user):
        return None
    visible = await visible_artifact_ids(db, report, user)
    if visible is None:
        return None
    if not visible:
        return set()

    contents = (await db.execute(
        select(ArtifactVersion.content).where(
            ArtifactVersion.artifact_id.in_(list(visible)),
            ArtifactVersion.deleted_at.is_(None),
        )
    )).scalars().all()
    query_ids = set(await query_ids_for_contents(db, contents))
    if not query_ids:
        return set()

    # A filter's options come from another query of the report, referenced
    # by id (or, on older data, by title — the same refs the page resolves).
    refs: set[str] = set()
    for params in (await db.execute(
        select(Query.parameters).where(Query.id.in_(list(query_ids)))
    )).scalars().all():
        for p in params or []:
            src = p.get("options_source") if isinstance(p, dict) else None
            if isinstance(src, dict) and src.get("query_id"):
                refs.add(str(src["query_id"]).strip())
    if refs:
        rows = (await db.execute(
            select(Query.id, Query.title).where(
                Query.report_id == str(report.id),
                Query.deleted_at.is_(None),
            )
        )).all()
        lowered = {r.lower() for r in refs}
        query_ids.update(
            str(qid) for qid, title in rows
            if str(qid) in refs or (title or "").lower() in lowered
        )
    return query_ids


async def assert_can_view_artifact(db, report: Report, artifact_id: str, user) -> None:
    """Raise 401/403/404 unless the caller may open this artifact of the report."""
    from app.services.bow_source_access import assert_read
    await assert_read(db, getattr(report, "bow_source_access", None), user)
    artifacts = await _live_artifacts(db, report.id, [artifact_id])
    if not artifacts:
        raise HTTPException(status_code=404, detail="Not found")
    if _is_owner(report, user) or await project_grants_view(db, report, user):
        return
    allowed, status = await _decide(db, report, user, artifacts)
    if str(artifact_id) not in allowed:
        _raise(status)


async def assert_can_view_any(db, report: Report, user) -> None:
    """Raise 401/403/404 unless the caller may open at least one live
    artifact of the report (the gate for report-level /r surfaces)."""
    from app.services.bow_source_access import assert_read
    await assert_read(db, getattr(report, "bow_source_access", None), user)
    if _is_owner(report, user) or await project_grants_view(db, report, user):
        return
    allowed, status = await _decide(db, report, user, await _live_artifacts(db, report.id))
    if not allowed:
        _raise(status)


async def can_view(db, report: Report, user, artifact_id: Optional[str] = None) -> bool:
    """Grant-only check for the permission decorator, which has already run
    the org, bow, admin and project gates: owner, or the artifact's
    visibility/grants (any live artifact when artifact_id is None). Reports
    without live artifacts fall back to the report-level fields."""
    if _is_owner(report, user):
        return True
    if not await report_has_live_artifacts(db, report.id):
        from app.services.report_service import ReportService
        try:
            await ReportService()._check_visibility(db, report, "artifact_visibility", user)
        except HTTPException:
            return False
        return True
    ids = [artifact_id] if artifact_id else None
    artifacts = await _live_artifacts(db, report.id, ids)
    allowed, _ = await _decide(db, report, user, artifacts)
    return bool(allowed)


async def sync_report_aggregate(db, report: Report, *, reset_when_empty: bool = False) -> None:
    """Keep the report-level dashboard fields in step with the artifacts:
    artifact_visibility = the most open artifact visibility, the report_shares
    'artifact' rows = the union of the 'shared' artifacts' grants, and the
    legacy published/draft status to match. No-op for a report without live
    artifacts — its report-level setting is still the source of truth —
    unless reset_when_empty: when the last dashboard is deleted the report
    goes back to private, so its queries do not fall back open to the
    grantees of dashboards that no longer exist."""
    from app.models.report_share import ReportShare

    artifacts = await _live_artifacts(db, report.id)
    if not artifacts and not reset_when_empty:
        return
    top = most_open(v for _, v in artifacts)
    report.artifact_visibility = top
    if report.status != "archived":
        report.status = "published" if top != "none" else "draft"

    shared_ids = [a for a, v in artifacts if v == "shared"]
    grants: set[tuple[Optional[str], Optional[str]]] = set()
    if shared_ids:
        rows = (await db.execute(
            select(ArtifactShare.user_id, ArtifactShare.group_id).where(
                ArtifactShare.artifact_id.in_(shared_ids),
                ArtifactShare.deleted_at.is_(None),
            )
        )).all()
        grants = {(str(u) if u else None, str(g) if g else None) for u, g in rows}

    await db.execute(
        delete(ReportShare).where(
            ReportShare.report_id == str(report.id),
            ReportShare.share_type == "artifact",
        )
    )
    for user_id, group_id in grants:
        db.add(ReportShare(
            report_id=str(report.id), user_id=user_id, group_id=group_id, share_type="artifact",
        ))


async def apply_report_setting_to_artifacts(db, report: Report) -> None:
    """Copy the report-level dashboard setting onto every live artifact.

    For the callers that still set sharing for the whole report at once
    (PUT /reports/{id}/visibility/artifact with a visibility, the legacy
    publish toggle and status update): every artifact gets the report's
    visibility and, when 'shared', exactly the report's grants."""
    from app.models.report_share import ReportShare

    artifacts = await _live_artifacts(db, report.id)
    if not artifacts:
        return
    ids = [a for a, _ in artifacts]
    visibility = report.artifact_visibility or "none"
    rows = (await db.execute(
        select(Artifact).where(Artifact.id.in_(ids))
    )).scalars().all()
    for artifact in rows:
        artifact.visibility = visibility

    await db.execute(delete(ArtifactShare).where(ArtifactShare.artifact_id.in_(ids)))
    if visibility != "shared":
        return
    grants = (await db.execute(
        select(ReportShare.user_id, ReportShare.group_id).where(
            ReportShare.report_id == str(report.id),
            ReportShare.share_type == "artifact",
            ReportShare.deleted_at.is_(None),
        )
    )).all()
    for artifact_id in ids:
        for user_id, group_id in grants:
            db.add(ArtifactShare(
                artifact_id=artifact_id, report_id=str(report.id),
                user_id=str(user_id) if user_id else None,
                group_id=str(group_id) if group_id else None,
            ))

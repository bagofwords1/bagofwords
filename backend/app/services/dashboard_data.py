"""What a dashboard's reader gets once the door opened (phase 4, part B).

The published side reads only the release: its content, its copied queries
(artifact_release_id), their default steps, its files and previews. Data goes
through today's viewer policy (viewer_data_policy.resolve_step_data), asked
of the dashboard instead of a conversation: the owner is `artifacts.owner_id`,
"run on my behalf" is `artifacts.shared_run_identity`, the sources are the
release's (or, with none attached, the organization's the code names, as for
a conversation without sources), and monitoring access is the release's.

The draft side reads one dashboard's newest finished version and the queries
it needs (phase 3's collector), with the conversation's own policy -- what
/r gives the same person today, scoped to this dashboard.

Exports (`ReleaseRender`) embed per chart what the step route gives the same
reader, the rule PR #1304 set for /r.
"""
import uuid
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import lazyload, selectinload

from app.errors import AppError, ErrorCode
from app.models.artifact import Artifact, ArtifactVersion
from app.models.artifact_release import ArtifactRelease, artifact_release_data_source_association
from app.models.query import Query
from app.models.step import Step
from app.services.viewer_data_policy import StepDataResolution, redact_applied_params


def not_found() -> AppError:
    # One answer for "no such row" and "not this dashboard's row".
    return AppError.not_found(ErrorCode.ARTIFACT_NOT_FOUND)


def _content(release_or_version) -> dict:
    content = release_or_version.content or {}
    return content if isinstance(content, dict) else {}


# ── the withholding policy, asked of a release ───────────────────────────

async def release_data_source_ids(db, release_id) -> list[str]:
    rows = (await db.execute(
        select(artifact_release_data_source_association.c.data_source_id).where(
            artifact_release_data_source_association.c.artifact_release_id == str(release_id))
    )).all()
    return [str(r[0]) for r in rows]


async def release_run_data_source_ids(db, release: ArtifactRelease) -> list[str]:
    """The sources a release's runs use: its own, else (an "Auto" release)
    the organization's its copies' code names, else all of them -- the
    fallback a conversation without sources gets."""
    from app.services.viewer_data_policy import _org_fallback_ds_ids

    ds_ids = await release_data_source_ids(db, release.id)
    if ds_ids:
        return ds_ids
    codes = (await db.execute(
        select(Step.code).join(Query, Query.id == Step.query_id).where(Query.artifact_release_id == str(release.id))
    )).scalars().all()
    return await _org_fallback_ds_ids(db, release.organization_id, "\n".join(c for c in codes if c) or None)


async def release_snapshot_withheld(db, artifact: Artifact, release: ArtifactRelease, *,
                                    code: str | None = None) -> bool:
    """snapshot_withheld_for_viewers, for a release: True when readers other
    than the owner must not get the copied snapshot."""
    from app.services.viewer_data_policy import (
        _any_rls_relation,
        _any_user_scoped_connection,
        _org_fallback_ds_ids,
    )

    ds_ids = await release_data_source_ids(db, release.id)
    if not ds_ids:
        ds_ids = await _org_fallback_ds_ids(db, release.organization_id, code)
    if await _any_rls_relation(db, ds_ids):
        return True
    if (artifact.shared_run_identity or "viewer") != "viewer":
        return False
    return await _any_user_scoped_connection(db, ds_ids)


async def resolve_release_step(db, step: Step, artifact: Artifact, release: ArtifactRelease,
                               user) -> StepDataResolution:
    """resolve_step_data for a release copy: the owner's snapshot, else the
    reader's own run, else the snapshot unless it is one identity's."""
    from app.models.step_user_result import StepUserResult
    from app.services.access_errors import viewer_error_code
    from app.services.bow_source_access import can_read
    from app.services.dashboard_access import holds_snapshot
    from app.services.identity_taint import step_identity_scope

    if release.bow_source_access and not await can_read(db, release.bow_source_access, user):
        return StepDataResolution(data={}, withheld=True)
    shared = step.data or {}
    if await holds_snapshot(db, artifact, release, user):
        return StepDataResolution(data=shared)

    viewer_result = None
    if user is not None:
        row = (await db.execute(
            select(StepUserResult).options(lazyload("*")).where(
                StepUserResult.step_id == str(step.id),
                StepUserResult.user_id == str(user.id),
            ).order_by(StepUserResult.last_run_at.desc())
        )).scalars().first()
        if row is not None:
            viewer_result = {
                "status": row.status,
                "status_reason": row.status_reason,
                "error_code": viewer_error_code(row.status, row.status_reason),
                "executed_as": row.executed_as,
                "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
            }
            if row.status == "success" and row.data:
                return StepDataResolution(data=row.data, viewer_result=viewer_result)

    tainted, _ = await step_identity_scope(db, step)
    if tainted or await release_snapshot_withheld(db, artifact, release, code=step.code):
        return StepDataResolution(data={}, withheld=True, viewer_result=viewer_result)
    return StepDataResolution(data=shared, viewer_result=viewer_result)


async def any_copy_tainted(db, release: ArtifactRelease) -> bool:
    """Whether some copied step holds one identity's slice (identity params,
    its own or through what it loads). Files that bake every chart's rows in --
    a deck, its PDF, its slide images -- then picture the owner's slice."""
    from app.services.identity_taint import step_identity_scope

    steps = (await db.execute(
        select(Step).options(lazyload("*")).join(Query, Query.id == Step.query_id)
        .where(Query.artifact_release_id == str(release.id))
    )).scalars().all()
    for step in steps:
        tainted, _ = await step_identity_scope(db, step)
        if tainted:
            return True
    return False


# ── rows of the release ──────────────────────────────────────────────────

async def release_queries(db, release: ArtifactRelease) -> list[Query]:
    """The copies a page lists: live, with a successful default step -- as
    the conversation's public list does, scoped to the release."""
    return list((await db.execute(
        select(Query).join(Step, Step.id == Query.default_step_id)
        .options(lazyload("*"), selectinload(Query.visualizations).options(lazyload("*")))
        .where(Query.artifact_release_id == str(release.id), Query.deleted_at.is_(None),
               Step.status == "success")
    )).scalars().all())


async def release_query(db, release: ArtifactRelease, query_id: str) -> Query:
    query = (await db.execute(
        select(Query).options(lazyload("*")).where(
            Query.id == str(query_id), Query.artifact_release_id == str(release.id), Query.deleted_at.is_(None))
    )).scalar_one_or_none()
    if query is None:
        raise not_found()
    return query


async def default_step(db, query: Query) -> Step | None:
    """The default step when it succeeded, else the latest successful one."""
    if query.default_step_id:
        step = (await db.execute(
            select(Step).options(lazyload("*")).where(Step.id == query.default_step_id, Step.status == "success")
        )).scalar_one_or_none()
        if step is not None:
            return step
    return (await db.execute(
        select(Step).options(lazyload("*")).where(Step.query_id == query.id, Step.status == "success")
        .order_by(Step.created_at.desc()).limit(1)
    )).scalar_one_or_none()


async def pending_step(db, query: Query) -> Step | None:
    """The default step while it waits to run with its new owner's
    credentials (a joining editor's copy, collab01)."""
    from app.services.fork_service import FORK_PENDING_STATUS

    if not query.default_step_id:
        return None
    return (await db.execute(
        select(Step).options(lazyload("*")).where(Step.id == query.default_step_id,
                                                  Step.status == FORK_PENDING_STATUS)
    )).scalar_one_or_none()


async def public_step(db, step: Step, query: Query, resolution: StepDataResolution, *,
                      organization_id, user):
    """The step as /r serves it: no code when withheld, identity values
    redacted, code visibility by the reader's role."""
    from app.core.code_visibility import can_view_code, set_code_visibility
    from app.core.permission_resolver import resolve_permissions
    from app.schemas.step_schema import PublicStepSchema

    if user is not None:
        set_code_visibility(can_view_code(await resolve_permissions(db, str(user.id), str(organization_id))))
    else:
        set_code_visibility(True)
    view = step.view if isinstance(step.view, dict) else (step.view.dict() if step.view else {})
    return PublicStepSchema(
        id=step.id, title=step.title, type=step.type,
        code="" if resolution.withheld else step.code,
        data_model=step.data_model or {}, data=resolution.data, view=view,
        viewer_result=resolution.viewer_result, snapshot_withheld=resolution.withheld,
        applied_params=redact_applied_params(getattr(step, "applied_params", None), query.parameters,
                                             withheld=resolution.withheld),
    )


def file_ids(content: dict) -> set[str]:
    return {str(f.get("id")) for f in (content.get("files") or []) if isinstance(f, dict) and f.get("id")}


# ── the draft of one dashboard ───────────────────────────────────────────

async def draft_version(db, artifact: Artifact, *, light: bool = False,
                        draft_id: str | None = None) -> ArtifactVersion | None:
    """The dashboard's draft: its newest completed version. `light`: without
    the version's eager relationships (its conversation, user, ...).

    Collaboration (collab01): a draft is the implicit one (`draft_id` None)
    or a named one, and its versions count only in a live conversation
    editing the dashboard -- when the dashboard has one at all (with none,
    every version counts, as before)."""
    from app.services.dashboard_collab import editing_report_ids

    stmt = select(ArtifactVersion)
    if light:
        stmt = stmt.options(lazyload("*"))
    stmt = stmt.where(
        ArtifactVersion.artifact_id == str(artifact.id),
        ArtifactVersion.deleted_at.is_(None),
        ArtifactVersion.status == "completed",
        ArtifactVersion.draft_id.is_(None) if draft_id is None else ArtifactVersion.draft_id == str(draft_id),
    )
    editing = await editing_report_ids(db, artifact)
    if editing:
        stmt = stmt.where(ArtifactVersion.report_id.in_(editing))
    return (await db.execute(stmt.order_by(ArtifactVersion.version.desc()).limit(1))).scalar_one_or_none()


async def draft_query_ids(db, version: ArtifactVersion | None) -> set[str]:
    """What the dashboard needs from its conversation: shown queries, filter
    option queries and loaded steps (phase 3's collector)."""
    if version is None:
        return set()
    from app.services.dashboard_copy import collect_dashboard_deps

    # A conversation that joined the dashboard (collab01) shows its copies
    # while they still run with its user's credentials; any other draft as
    # before (a fork's pending hydration stays out of it).
    from app.models.artifact_release import ArtifactConversation

    joined = (await db.execute(select(ArtifactConversation.id).where(
        ArtifactConversation.report_id == str(version.report_id)).limit(1))).first() is not None
    deps = await collect_dashboard_deps(db, version, include_pending=joined)
    return {str(q) for q in deps.query_ids} | {str(q) for q in deps.hidden_query_ids}


async def version_report(db, version: ArtifactVersion):
    """The conversation a draft version was made in (collab01: one of
    several editing the dashboard)."""
    from app.models.report import Report

    report = (await db.execute(
        select(Report).options(lazyload("*")).where(Report.id == str(version.report_id))
    )).scalar_one_or_none() if version is not None else None
    if report is None:
        raise not_found()
    return report


async def draft_report(db, artifact: Artifact):
    from app.models.report import Report

    report = (await db.execute(
        select(Report).options(lazyload("*")).where(Report.id == str(artifact.report_id))
    )).scalar_one_or_none() if artifact.report_id else None
    if report is None:
        raise not_found()
    return report


# ── exports of a release ─────────────────────────────────────────────────

class ReleaseRender(SimpleNamespace):
    """A release in the shape the export services read off an artifact
    version, carrying the rows one reader may see.

    `id` names the output files: one per request, because two readers'
    files hold different rows and one reader's may be downloading while the
    next is rendered. The caller deletes the file once it is sent.
    """

    @classmethod
    def build(cls, artifact: Artifact, release: ArtifactRelease, user) -> "ReleaseRender":
        async def rows_for(db, step: Step, query: Query) -> dict[str, Any]:
            resolution = await resolve_release_step(db, step, artifact, release, user)
            return {"data": resolution.data,
                    "applied_params": redact_applied_params(getattr(step, "applied_params", None),
                                                            query.parameters, withheld=resolution.withheld)}

        return cls(
            id=f"release-{release.id}-{uuid.uuid4().hex}",
            artifact_id=str(artifact.id), artifact_release_id=str(release.id), report_id=None,
            organization_id=str(artifact.organization_id), title=artifact.title, mode=artifact.mode or "page",
            content=_content(release), pptx_path=release.pptx_path, status="completed", deleted_at=None,
            report_payload={"id": str(artifact.id), "title": artifact.title, "theme": release.theme_name},
            rows_for=rows_for,
        )

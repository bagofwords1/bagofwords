"""Readers chat about a published dashboard (phase 7, part C).

The conversation's viewer chat (ArtifactChatService, /r/{id}/chat), keyed on
the dashboard instead of the conversation:

- who: whoever may read the dashboard (/d access), signed in, a live member
  of its organization, with the dashboard's chat switched on;
- the thread: the reader's newest hidden artifact_chat report on the
  dashboard, found by reports.source_artifact_id. forked_from_id stays empty, so the
  conversation's /r chat (keyed on it) never finds these threads;
- the agents: the dashboard's chat_data_source_ids, else the release's own
  sources (an "Auto" release: only those its copies' code names, never the
  whole organization), intersected with what the reader may use, live
  only. [] is data-only;
- the model: the dashboard's chat_model_id; none or ORG_DEFAULT_CHAT_MODEL
  pin nothing, so the organization's model resolves at run time (the
  conversation it was made in may be gone);
- the context: the live release -- its title and, in data-only scope, its
  charts with the rows this reader may see (ReleaseRender), never the stored
  snapshot as such.

It outlives the conversation: nothing here reads it.
"""
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.models.artifact import Artifact
from app.models.artifact_release import ArtifactRelease
from app.models.report import Report
from app.services.artifact_chat_service import MAX_CONTEXT_ROWS_PER_VIZ, artifact_chat_service


async def open_chat(db, ref: str, user) -> tuple[Artifact, ArtifactRelease]:
    """The dashboard and its live release, if `user` may chat about it;
    otherwise 401 (anonymous), 403 (chat off, or not a member) or the /d
    refusal."""
    from app.services.dashboard_access import _is_member, open_published

    artifact, release = await open_published(db, ref, user)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not artifact.chat_enabled:
        raise HTTPException(status_code=403, detail="Chat is not enabled on this dashboard")
    if not await _is_member(db, user, artifact.organization_id):
        raise HTTPException(status_code=403, detail="Chat is available to members of this workspace only")
    return artifact, release


async def _release_sources(db, release: ArtifactRelease) -> set[str]:
    """The sources the release itself uses: its own, else (an "Auto"
    release) those its copies' code names -- never every source of the
    organization, as the /r chat never widens either."""
    from app.models.data_source import DataSource
    from app.models.query import Query
    from app.models.step import Step
    from app.services.dashboard_data import release_data_source_ids

    own = set(await release_data_source_ids(db, release.id))
    if own:
        return own
    codes = "\n".join(c for (c,) in (await db.execute(
        select(Step.code).join(Query, Query.default_step_id == Step.id)
        .where(Query.artifact_release_id == str(release.id)))).all() if c)
    rows = (await db.execute(select(DataSource.id, DataSource.name).where(
        DataSource.organization_id == str(release.organization_id), DataSource.deleted_at.is_(None)))).all()
    return {str(i) for i, name in rows if name and str(name) in codes}


async def effective_agent_ids(db, artifact: Artifact, release: ArtifactRelease, organization, user) -> list[str]:
    from app.ai.tools.implementations.agent_focus_common import accessible_agents
    from app.services.data_source_service import DataSourceService

    chosen = artifact.chat_data_source_ids
    allowed = await _release_sources(db, release) if chosen is None else {str(x) for x in chosen}
    if not allowed:
        return []
    return [str(ds.id) for ds in await accessible_agents(db, organization, user)
            if str(ds.id) in allowed and DataSourceService.is_execution_live(ds)]


def chat_model_id(artifact: Artifact) -> str | None:
    from app.services.artifact_chat_service import ORG_DEFAULT_CHAT_MODEL

    chosen = artifact.chat_model_id
    return None if not chosen or chosen == ORG_DEFAULT_CHAT_MODEL else chosen


async def find_thread(db, artifact: Artifact, user) -> Report | None:
    """The reader's newest thread: a reader may hold several (the artifact's
    own conversations, services/artifact_conversations.py); the bubble
    continues the latest."""
    return (await db.execute(
        select(Report).where(
            Report.report_type == "artifact_chat", Report.source_artifact_id == str(artifact.id),
            Report.user_id == str(user.id), Report.deleted_at.is_(None),
        ).order_by(Report.created_at.desc()).limit(1)
    )).scalars().first()


async def resolve_thread(db, artifact: Artifact, user, agent_ids: list[str]) -> Report:
    """This reader's thread on this dashboard, its model and agents re-synced
    (owner changes apply on the next message). Committed."""
    thread = await find_thread(db, artifact, user)
    if thread is None:
        thread = Report(
            title=f"Chat: {artifact.title or 'Untitled dashboard'}",
            slug=f"artifact-chat-{uuid.uuid4().hex[:12]}", status="draft", report_type="artifact_chat",
            mode="chat", model_id=chat_model_id(artifact), user_id=str(user.id),
            organization_id=str(artifact.organization_id), source_artifact_id=str(artifact.id),
        )
        db.add(thread)
        await db.flush()
    else:
        thread.model_id = chat_model_id(artifact)
    await artifact_chat_service.sync_roster(db, thread, agent_ids)
    await db.commit()
    return thread


async def build_context(db, artifact: Artifact, release: ArtifactRelease, agent_ids: list[str], user) -> dict:
    """The platform context of a message: the dashboard, and in data-only
    scope its live release's charts with the rows this reader may see."""
    from app.services.artifact_payload import collect_visualizations
    from app.services.dashboard_data import ReleaseRender

    context: dict = {"source_report_title": artifact.title, "artifact_title": artifact.title,
                     "scope": "agents" if agent_ids else "data_only"}
    if not agent_ids:
        charts = await collect_visualizations(db, ReleaseRender.build(artifact, release, user))
        context["visualizations"] = [{
            "title": v.get("title"), "columns": v.get("columns") or [],
            "rows": (v.get("rows") or [])[:MAX_CONTEXT_ROWS_PER_VIZ], "total_rows": len(v.get("rows") or []),
        } for v in charts]
    return context

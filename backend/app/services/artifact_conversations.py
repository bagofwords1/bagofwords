"""Conversations an artifact holds with its viewer (``bow.conversations``).

A conversation is the viewer's own hidden ``artifact_chat`` report on the
artifact (reports.source_artifact_id), the same kind of thread the dashboard's
chat bubble uses (services/dashboard_chat.py). It is the artifact's own
capability, not the bubble's, so the bubble's settings (Allow chat, its
"dashboard data only" scope) do not limit it:

* who: whoever may read the published artifact, signed in (an app session or
  an embed session), a live member of its organization; and its editors on
  the draft, before it is published, so they can try the app they build;
* the agents: Auto by default, like a normal BOW chat: every agent the
  viewer may use, resolved per message. Narrowed when the owner lists agents
  in the artifact's settings (chat_data_source_ids, a non-empty list), else
  when the report the artifact was built from attached agents explicitly (the
  release's sources; on a draft, its editing conversations'). An Auto report
  narrows nothing. A conversation may also name a subset when it starts
  (kept in its focused_data_source_ids). Viewer access applies on every
  message, so a revoked agent drops out at once; never "dashboard data only"
  (that is the bubble's option): no agent means the conversation is refused;
* the model: the owner's pin (chat_model_id), else the viewer's own default
  (personal, group, organization) with the organization's Auto routing. A
  pin the viewer may not use falls back to their default; policy() says which
  model runs;
* the context: the live release, the rows this viewer may see.

A viewer may hold any number of conversations on an artifact and sees only
their own; nobody else's are listed, opened or continued.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import AsyncIterator, Optional

from sqlalchemy import select

from app.errors import AppError, ErrorCode
from app.models.report import Report, report_data_source_association
from app.services import dashboard_chat
from app.services.artifact_chat_service import artifact_chat_service

MAX_TITLE = 120
PAGE_LIMIT = 50


async def open_for(db, artifact_id: str, user):
    """(artifact, release or None, organization) if ``user`` may hold
    conversations here. ``release`` is None only for an editor trying the
    unpublished draft."""
    from app.models.organization import Organization
    from app.services import dashboard_access

    if user is None:
        raise AppError.unauthorized(ErrorCode.EMBED_SESSION_INVALID, "Sign in to hold a conversation")
    artifact = await dashboard_access.resolve(db, artifact_id)
    release = await dashboard_access.published_release(db, artifact)
    if release is None and await dashboard_access.can_edit(db, artifact, user):
        artifact = await dashboard_access.open_draft(db, artifact_id, user)
    else:
        artifact, release = await dashboard_access.open_published(db, artifact_id, user)
    if not await dashboard_access._is_member(db, user, artifact.organization_id):
        raise AppError.forbidden(ErrorCode.CONVERSATION_UNAVAILABLE, "Conversations are for members of this workspace")
    organization = await db.get(Organization, str(artifact.organization_id))
    return artifact, release, organization


async def _explicit_agents(db, artifact, release) -> set[str]:
    """Agents the report attached explicitly (an Auto report attaches none):
    the release's own; on a draft, its editing conversations'."""
    if release is not None:
        from app.services.dashboard_data import release_data_source_ids

        return set(await release_data_source_ids(db, release.id))
    from app.services.dashboard_collab import editing_report_ids

    reports = [str(r) for r in await editing_report_ids(db, artifact)] or [str(artifact.report_id)]
    return {str(x) for x in (await db.execute(select(report_data_source_association.c.data_source_id).where(
        report_data_source_association.c.report_id.in_(reports)))).scalars().all()}


async def agent_limit(db, artifact, release) -> Optional[set[str]]:
    """The agents an app may use at most, or None for Auto."""
    chosen = artifact.chat_data_source_ids
    if isinstance(chosen, list) and chosen:
        return {str(x) for x in chosen}
    return (await _explicit_agents(db, artifact, release)) or None


async def allowed_agent_ids(db, artifact, release, organization, user) -> list[str]:
    """The agents this viewer may talk to through this artifact, live."""
    from app.ai.tools.implementations.agent_focus_common import accessible_agents
    from app.services.data_source_service import DataSourceService

    limit = await agent_limit(db, artifact, release)
    return [str(ds.id) for ds in await accessible_agents(db, organization, user)
            if DataSourceService.is_execution_live(ds) and (limit is None or str(ds.id) in limit)]


async def resolved_model(db, artifact, organization, user) -> Optional[dict]:
    """The model a message from this viewer runs on, by the same resolution a
    run uses (LLMService.get_default_model_for_report): the owner's pin when it
    is live and the viewer may use it, else the viewer's own default."""
    from types import SimpleNamespace

    from app.services.llm_service import LLMService

    pinned_id = dashboard_chat.chat_model_id(artifact)
    model = await LLMService().get_default_model_for_report(db, organization, user,
                                                           SimpleNamespace(model_id=pinned_id))
    if model is None:
        return None
    source = "default" if not pinned_id else ("pinned" if str(model.id) == str(pinned_id) else "fallback")
    return {"id": str(model.id), "name": model.name or model.model_id, "source": source}


def _no_agents() -> AppError:
    return AppError.forbidden(ErrorCode.CONVERSATION_NO_AGENTS, "No agent is available to you here")


async def policy(db, artifact, release, organization, user) -> dict:
    from app.models.data_source import DataSource

    agent_ids = await allowed_agent_ids(db, artifact, release, organization, user)
    model = await resolved_model(db, artifact, organization, user)
    if not agent_ids:
        return {"available": False, "reason": "no_agents", "agents": [], "model": model}
    names = dict((await db.execute(select(DataSource.id, DataSource.name).where(
        DataSource.id.in_(agent_ids)))).all())
    limited = await agent_limit(db, artifact, release) is not None
    return {"available": True, "agents_mode": "limited" if limited else "auto",
            "agents": [{"id": a, "name": names.get(a, "")} for a in agent_ids], "model": model}


def _summary(thread: Report, agent_ids: list[str] | None = None) -> dict:
    out = {"id": str(thread.id), "title": thread.title,
           "created_at": thread.created_at.isoformat() if thread.created_at else None,
           "updated_at": (thread.updated_at or thread.created_at).isoformat() if thread.created_at else None}
    if agent_ids is not None:
        out["agents"] = agent_ids
    return out


async def _roster(db, thread: Report) -> list[str]:
    return [str(x) for x in (await db.execute(select(report_data_source_association.c.data_source_id).where(
        report_data_source_association.c.report_id == str(thread.id)))).scalars().all()]


async def list_conversations(db, artifact, user, *, limit: int, before: Optional[str]) -> dict:
    query = select(Report).where(
        Report.report_type == "artifact_chat", Report.source_artifact_id == str(artifact.id),
        Report.user_id == str(user.id), Report.deleted_at.is_(None),
    )
    if before:
        try:
            query = query.where(Report.created_at < datetime.fromisoformat(before))
        except ValueError:
            raise AppError.bad_request(ErrorCode.CONVERSATION_NOT_FOUND, "Invalid cursor", field="before")
    rows = (await db.execute(query.order_by(Report.created_at.desc()).limit(limit + 1))).scalars().all()
    items = [_summary(t) for t in rows[:limit]]
    return {"items": items, "next_before": items[-1]["created_at"] if len(rows) > limit else None}


async def own(db, artifact, user, conversation_id: str) -> Report:
    thread = (await db.execute(select(Report).where(
        Report.id == str(conversation_id), Report.report_type == "artifact_chat",
        Report.source_artifact_id == str(artifact.id), Report.user_id == str(user.id),
        Report.deleted_at.is_(None)))).scalar_one_or_none()
    if thread is None:
        raise AppError.not_found(ErrorCode.CONVERSATION_NOT_FOUND, "Conversation not found")
    return thread


def _title(value: Optional[str], artifact) -> str:
    title = " ".join((value or "").split())[:MAX_TITLE]
    return title or f"Chat: {artifact.title or 'Untitled'}"


async def create(db, artifact, release, organization, user, *, title: Optional[str],
                 agent_ids: Optional[list[str]]) -> dict:
    allowed = await allowed_agent_ids(db, artifact, release, organization, user)
    if not allowed:
        raise _no_agents()
    narrowed = None
    if agent_ids:
        narrowed = list(dict.fromkeys(str(a) for a in agent_ids))
        if set(narrowed) - set(allowed):
            raise AppError.forbidden(ErrorCode.CONVERSATION_AGENT_FORBIDDEN, "You can't use that agent here")
    thread = Report(
        title=_title(title, artifact), slug=f"artifact-chat-{uuid.uuid4().hex[:12]}", status="draft",
        report_type="artifact_chat", mode="chat", model_id=dashboard_chat.chat_model_id(artifact),
        user_id=str(user.id), organization_id=str(artifact.organization_id), source_artifact_id=str(artifact.id),
        focused_data_source_ids=narrowed,
    )
    db.add(thread)
    await db.flush()
    roster = narrowed or allowed
    await artifact_chat_service.sync_roster(db, thread, roster)
    await db.commit()
    await db.refresh(thread)
    return _summary(thread, roster)


async def describe(db, thread: Report) -> dict:
    return _summary(thread, await _roster(db, thread))


async def rename(db, thread: Report, artifact, title: str) -> dict:
    thread.title = _title(title, artifact)
    await db.commit()
    await db.refresh(thread)
    return await describe(db, thread)


async def archive(db, thread: Report) -> None:
    thread.deleted_at = datetime.utcnow()
    await db.commit()


async def prepare_message(db, artifact, release, organization, user, thread: Report) -> dict:
    """Re-apply the policy before a message: the agents (Auto re-resolved for
    this viewer, or the conversation's own subset, both ∩ what is allowed now),
    the model, and the live context. Returns the platform context."""
    allowed = await allowed_agent_ids(db, artifact, release, organization, user)
    narrowed = thread.focused_data_source_ids or None
    keep = [a for a in narrowed if a in set(allowed)] if narrowed else allowed
    if not keep:
        # Never fall back to "dashboard data only": that is a different
        # conversation, and the app would answer as if it had no data access.
        raise _no_agents()
    thread.model_id = dashboard_chat.chat_model_id(artifact)
    await artifact_chat_service.sync_roster(db, thread, keep)
    await db.commit()
    if release is None:  # an editor trying the draft: no published rows to hand over
        return {"source_report_title": artifact.title, "artifact_title": artifact.title, "scope": "agents",
                "surface": "app"}
    return {**await dashboard_chat.build_context(db, artifact, release, keep, user), "surface": "app"}


def _texts(blocks) -> tuple[str, list[str]]:
    """(the answer, the narration before it): the last block with text is
    the reply; earlier ones are the agent's steps along the way."""
    said = [b["content"] for b in sorted(blocks or [], key=lambda b: (b.get("block_index") or 0)) if b.get("content")]
    return (said[-1] if said else ""), said[:-1]


def _tools(blocks) -> list[dict]:
    out = []
    for b in sorted(blocks or [], key=lambda b: (b.get("block_index") or 0)):
        te = b.get("tool_execution")
        if te:
            out.append({"name": te.get("tool_name"), "title": (te.get("arguments_json") or {}).get("title"),
                        "status": te.get("status") or b.get("status")})
    return out


async def messages(db, thread: Report, organization, user, *, limit: int, before: Optional[str]) -> dict:
    from app.services.completion_service import CompletionService

    raw = await CompletionService().get_completions_v2(db, str(thread.id), organization, user, limit=limit,
                                                       before=before)
    data = raw.model_dump() if hasattr(raw, "model_dump") else dict(raw)
    items = []
    for c in data.get("completions") or []:
        role = "user" if c.get("role") == "user" else "assistant"
        items.append({
            "id": str(c.get("id")), "role": role, "status": c.get("status"),
            "created_at": c["created_at"].isoformat() if hasattr(c.get("created_at"), "isoformat") else c.get("created_at"),
            "text": ((c.get("prompt") or {}).get("content") or "") if role == "user" else _texts(c.get("completion_blocks"))[0],
            **({"steps": _texts(c.get("completion_blocks"))[1], "tools": _tools(c.get("completion_blocks"))}
               if role == "assistant" else {}),
        })
    items.sort(key=lambda m: m["created_at"] or "")
    return {"items": items, "next_before": data.get("next_before")}


async def ndjson(sse: AsyncIterator) -> AsyncIterator[bytes]:
    """The completion's SSE stream as the artifact SDK's NDJSON events:
    started, tool, text_delta, text, completed (exactly once, last) or error."""
    buffer = ""
    tools: dict[str, str] = {}
    finished = False

    def line(event: dict) -> bytes:
        return (json.dumps(event, default=str) + "\n").encode()

    async for chunk in sse:
        buffer += chunk.decode() if isinstance(chunk, (bytes, bytearray)) else str(chunk)
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            data_line = next((x for x in frame.split("\n") if x.startswith("data: ")), None)
            if data_line is None:
                continue
            try:
                evt = json.loads(data_line[6:])
            except ValueError:
                continue
            kind, data = evt.get("event"), evt.get("data") or {}
            if kind == "completion.started":
                yield line({"type": "started", "message_id": data.get("system_completion_id"),
                            "prompt_id": evt.get("completion_id")})
            elif kind == "block.upsert" and (data.get("block") or {}).get("tool_execution"):
                block = data["block"]
                te = block["tool_execution"]
                status = te.get("status") or block.get("status") or ""
                if tools.get(block.get("id")) != status:
                    tools[block.get("id")] = status
                    yield line({"type": "tool", "id": block.get("id"), "name": te.get("tool_name"),
                                "title": (te.get("arguments_json") or {}).get("title"), "status": status})
            elif kind == "block.delta.token" and data.get("field") == "content" and data.get("token"):
                yield line({"type": "text_delta", "block": data.get("block_id"), "text": str(data["token"])})
            elif kind == "block.delta.text" and data.get("field") == "content" and data.get("text"):
                yield line({"type": "text", "block": data.get("block_id"), "text": str(data["text"])})
            elif kind == "completion.finished" and not finished:
                finished = True
                yield line({"type": "completed", "status": data.get("status") or "success"})
            elif kind == "completion.error" and not finished:
                finished = True
                yield line({"type": "error", "code": "UNAVAILABLE",
                            "message": str(data.get("error") or data.get("message") or "The answer failed")})
    if not finished:
        yield line({"type": "error", "code": "INTERRUPTED", "message": "The answer was interrupted"})

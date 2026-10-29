"""Conversations an artifact holds with its viewer (``bow.conversations``).

A conversation is the viewer's own hidden ``artifact_chat`` report on the
artifact (reports.source_artifact_id), the same kind of thread the dashboard's
chat bubble uses (services/dashboard_chat.py), under the same policy:

* who: whoever may read the published artifact, signed in (an app session or
  an embed session), a live member of its organization, with the artifact's
  chat switched on;
* the agents: those the artifact's chat allows (chat_data_source_ids, else
  the release's own) that the viewer may use, live. A conversation may name a
  subset when it starts (a report can carry several agents); each message
  re-checks it, so a revoked agent drops out at once;
* the model: the artifact's chat_model_id;
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
    """(artifact, release, organization) if ``user`` may hold conversations here."""
    from fastapi import HTTPException

    from app.models.organization import Organization

    try:
        artifact, release = await dashboard_chat.open_chat(db, artifact_id, user)
    except HTTPException as e:
        if e.status_code == 403 and "not enabled" in str(e.detail):
            raise AppError.forbidden(ErrorCode.CONVERSATION_UNAVAILABLE, "Conversations are not turned on here")
        raise
    organization = await db.get(Organization, str(artifact.organization_id))
    return artifact, release, organization


async def policy(db, artifact, release, organization, user) -> dict:
    from app.models.data_source import DataSource

    agent_ids = await dashboard_chat.effective_agent_ids(db, artifact, release, organization, user)
    names = dict((await db.execute(select(DataSource.id, DataSource.name).where(
        DataSource.id.in_(agent_ids)))).all()) if agent_ids else {}
    return {"available": True, "scope": "agents" if agent_ids else "data_only",
            "agents": [{"id": a, "name": names.get(a, "")} for a in agent_ids]}


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
    allowed = await dashboard_chat.effective_agent_ids(db, artifact, release, organization, user)
    if agent_ids is None:
        chosen = allowed
    else:
        chosen = list(dict.fromkeys(str(a) for a in agent_ids))
        if set(chosen) - set(allowed):
            raise AppError.forbidden(ErrorCode.CONVERSATION_AGENT_FORBIDDEN, "You can't use that agent here")
    thread = Report(
        title=_title(title, artifact), slug=f"artifact-chat-{uuid.uuid4().hex[:12]}", status="draft",
        report_type="artifact_chat", mode="chat", model_id=dashboard_chat.chat_model_id(artifact),
        user_id=str(user.id), organization_id=str(artifact.organization_id), source_artifact_id=str(artifact.id),
    )
    db.add(thread)
    await db.flush()
    await artifact_chat_service.sync_roster(db, thread, chosen)
    await db.commit()
    await db.refresh(thread)
    return _summary(thread, chosen)


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
    """Re-apply the policy before a message: the thread's agents still allowed,
    the artifact's model, and the live context. Returns the platform context."""
    allowed = set(await dashboard_chat.effective_agent_ids(db, artifact, release, organization, user))
    current = await _roster(db, thread)
    keep = [a for a in current if a in allowed]
    if current and not keep:
        # Every agent it was started with is gone: it would silently become
        # data-only, which is a different conversation.
        raise AppError.forbidden(ErrorCode.CONVERSATION_AGENT_FORBIDDEN, "The agents of this conversation are no longer available to you")
    thread.model_id = dashboard_chat.chat_model_id(artifact)
    await artifact_chat_service.sync_roster(db, thread, keep)
    await db.commit()
    return await dashboard_chat.build_context(db, artifact, release, keep, user)


def _text(blocks) -> str:
    ordered = sorted(blocks or [], key=lambda b: (b.get("block_index") or 0))
    return "\n\n".join(b["content"] for b in ordered if b.get("content"))


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
            "text": ((c.get("prompt") or {}).get("content") or "") if role == "user" else _text(c.get("completion_blocks")),
            **({"tools": _tools(c.get("completion_blocks"))} if role == "assistant" else {}),
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

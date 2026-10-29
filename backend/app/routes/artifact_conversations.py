"""``bow.conversations``: the viewer's conversations with an artifact.

Reached from the artifact's sandbox only through the page's runtime broker
(frontend/composables/useArtifactRuntime.ts), like the rest of the runtime.
The viewer is the app session or an embed session (core/embed_auth.py); the
policy is in services/artifact_conversations.py (independent of the chat bubble).
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embed_auth import viewer_optional
from app.dependencies import get_async_db
from app.errors import AppError, ErrorCode
from app.models.user import User
from app.services import artifact_conversations as conversations
from app.services.artifact_admission import admit
from app.streaming.delivery import NO_BUFFER_HEADERS

router = APIRouter(prefix="/artifacts/{artifact_id}/runtime/conversations", tags=["artifact conversations"])

MESSAGES_PER_MINUTE = 6            # per viewer per artifact
ORG_MESSAGES_PER_MINUTE = 60       # per organization, all artifacts
READS_PER_MINUTE = 240


class ConversationCreate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    # A subset of the agents the artifact's chat allows; omitted: all of them.
    agents: Optional[List[str]] = Field(default=None, max_length=20)


class ConversationRename(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class MessageCreate(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


async def _viewer(artifact_id: str, request: Request, db: AsyncSession, user: Optional[User], category: str):
    if user is None:
        raise AppError.unauthorized(ErrorCode.EMBED_SESSION_INVALID, "Sign in to hold a conversation")
    artifact, release, organization = await conversations.open_for(db, artifact_id, user)
    if category == "send":
        await admit(db, [str(artifact.id), str(user.id), "conversation"], MESSAGES_PER_MINUTE)
        await admit(db, [str(organization.id), "conversation"], ORG_MESSAGES_PER_MINUTE)
    else:
        await admit(db, [str(artifact.id), str(user.id), "conversation-read"], READS_PER_MINUTE)
    return artifact, release, organization, user


@router.get("/policy")
async def conversation_policy(artifact_id: str, request: Request, db: AsyncSession = Depends(get_async_db),
                              user: Optional[User] = Depends(viewer_optional)):
    """Whether this viewer may hold conversations here, and with which agents."""
    if user is None:
        return {"available": False, "reason": "auth_required", "agents": []}
    try:
        artifact, release, organization, user = await _viewer(artifact_id, request, db, user, "read")
    except AppError as e:
        if e.error_code == ErrorCode.CONVERSATION_UNAVAILABLE.value:
            return {"available": False, "reason": "not_member", "agents": []}
        raise
    return await conversations.policy(db, artifact, release, organization, user)


@router.get("")
async def list_conversations(artifact_id: str, request: Request, limit: int = Query(20, ge=1, le=50),
                             before: Optional[str] = None, db: AsyncSession = Depends(get_async_db),
                             user: Optional[User] = Depends(viewer_optional)):
    artifact, _, _, user = await _viewer(artifact_id, request, db, user, "read")
    return await conversations.list_conversations(db, artifact, user, limit=limit, before=before)


@router.post("", status_code=201)
async def create_conversation(artifact_id: str, body: ConversationCreate, request: Request,
                              db: AsyncSession = Depends(get_async_db), user: Optional[User] = Depends(viewer_optional)):
    artifact, release, organization, user = await _viewer(artifact_id, request, db, user, "read")
    return await conversations.create(db, artifact, release, organization, user, title=body.title,
                                      agent_ids=body.agents)


@router.get("/{conversation_id}")
async def get_conversation(artifact_id: str, conversation_id: str, request: Request,
                           db: AsyncSession = Depends(get_async_db), user: Optional[User] = Depends(viewer_optional)):
    artifact, _, _, user = await _viewer(artifact_id, request, db, user, "read")
    return await conversations.describe(db, await conversations.own(db, artifact, user, conversation_id))


@router.patch("/{conversation_id}")
async def rename_conversation(artifact_id: str, conversation_id: str, body: ConversationRename, request: Request,
                              db: AsyncSession = Depends(get_async_db), user: Optional[User] = Depends(viewer_optional)):
    artifact, _, _, user = await _viewer(artifact_id, request, db, user, "read")
    thread = await conversations.own(db, artifact, user, conversation_id)
    return await conversations.rename(db, thread, artifact, body.title)


@router.delete("/{conversation_id}", status_code=204)
async def archive_conversation(artifact_id: str, conversation_id: str, request: Request,
                               db: AsyncSession = Depends(get_async_db), user: Optional[User] = Depends(viewer_optional)):
    artifact, _, _, user = await _viewer(artifact_id, request, db, user, "read")
    await conversations.archive(db, await conversations.own(db, artifact, user, conversation_id))
    return Response(status_code=204)


@router.get("/{conversation_id}/messages")
async def list_messages(artifact_id: str, conversation_id: str, request: Request,
                        limit: int = Query(30, ge=1, le=100), before: Optional[str] = None,
                        db: AsyncSession = Depends(get_async_db), user: Optional[User] = Depends(viewer_optional)):
    artifact, _, organization, user = await _viewer(artifact_id, request, db, user, "read")
    thread = await conversations.own(db, artifact, user, conversation_id)
    return await conversations.messages(db, thread, organization, user, limit=limit, before=before)


@router.post("/{conversation_id}/messages")
async def send_message(artifact_id: str, conversation_id: str, body: MessageCreate, request: Request,
                       db: AsyncSession = Depends(get_async_db), user: Optional[User] = Depends(viewer_optional)):
    """A message, answered by the conversation's agents. Streams NDJSON
    (started, tool, text_delta, text, result, then completed or error;
    completed adds results_incomplete: true when some results are missing)."""
    from app.schemas.completion_v2_schema import CompletionCreate, PromptSchema
    from app.services.completion_service import CompletionService

    artifact, release, organization, user = await _viewer(artifact_id, request, db, user, "send")
    thread = await conversations.own(db, artifact, user, conversation_id)
    context = await conversations.prepare_message(db, artifact, release, organization, user, thread)
    completion = CompletionCreate(prompt=PromptSchema(content=body.text.strip(), mode="chat", platform="artifact_chat",
                                                      platform_context=context), stream=True)
    sse = await CompletionService().create_completion_stream(db, str(thread.id), completion, user, organization)
    if not isinstance(sse, StreamingResponse):
        raise AppError.conflict(ErrorCode.CONVERSATION_LIMIT, "This conversation is still answering")
    return StreamingResponse(conversations.ndjson(sse.body_iterator, thread_id=str(thread.id),
                                                  organization_id=str(organization.id)),
                             media_type="application/x-ndjson", headers=NO_BUFFER_HEADERS)


@router.post("/{conversation_id}/messages/{message_id}/stop")
async def stop_message(artifact_id: str, conversation_id: str, message_id: str, request: Request,
                       db: AsyncSession = Depends(get_async_db), user: Optional[User] = Depends(viewer_optional)):
    from sqlalchemy import select

    from app.models.completion import Completion
    from app.services.completion_service import CompletionService

    artifact, _, organization, user = await _viewer(artifact_id, request, db, user, "read")
    thread = await conversations.own(db, artifact, user, conversation_id)
    found = (await db.execute(select(Completion.id).where(
        Completion.id == str(message_id), Completion.report_id == str(thread.id)))).first()
    if found is None:
        raise AppError.not_found(ErrorCode.CONVERSATION_NOT_FOUND, "Message not found")
    completion = await CompletionService().update_completion_sigkill(db, str(message_id), user, organization)
    return {"id": str(completion.id), "status": completion.status}

"""Conversations an artifact holds with its viewer (``bow.conversations``).

A conversation is the viewer's own hidden ``artifact_chat`` report on the
artifact (reports.source_artifact_id), the same kind of thread the dashboard's
chat bubble uses (services/dashboard_chat.py). It is the artifact's own
capability, not the bubble's, so the bubble's settings (Allow chat, its
"dashboard data only" scope) do not limit it:

* who: whoever may read the published artifact, signed in (an app session or
  an embed session), a live member of its organization; and its editors on
  the draft, before it is published, so they can try the app they build;
* the agents: the ones the artifact was built with (the release's, or the
  draft's conversation's) plus any the chat settings name, that the viewer
  may use, live. A conversation may name a subset when it starts (a report
  can carry several agents); each message re-checks it, so a revoked agent
  drops out at once;
* the model: the artifact's chat_model_id, else the organization's;
* the context: the live release, the rows this viewer may see.

A viewer may hold any number of conversations on an artifact and sees only
their own; nobody else's are listed, opened or continued.

The answer's data results (each successful create_data of the message: a
capped table and a validated chart suggestion, services/
artifact_conversation_results.py) reach the app as ``result`` events before
``completed``, and again as ``results`` on the assistant message in history.
When some could not be shown (a lookup that failed for good, a result not
stored in time or not stored as a success, the message budget, or PII
settings that cannot be read, which withholds them), ``completed`` and that
history message add ``results_incomplete: true``; the key is absent
otherwise. The stream decides it by accounting at the end (a result still
pending, a successful result tool whose step was never sent, one cut by the
budget, or one the final sweep could only send degraded), never from a lookup
outcome alone: a failure that a later lookup recovers, or a failed final sweep
after every result was already sent whole, is not one.
Its messages carry ``surface: "conversation"`` so the agent is told what the
app shows; the bubble's do not.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import AsyncIterator, Optional

from sqlalchemy import select

from app.errors import AppError, ErrorCode
from app.models.report import Report, report_data_source_association
from app.services import dashboard_chat
from app.services.artifact_chat_service import artifact_chat_service

logger = logging.getLogger(__name__)

MAX_TITLE = 120
PAGE_LIMIT = 50
RESULT_DEFER_SECONDS = 4.0  # longest wait for a result's rows after the answer finished
RESULT_POLL_SECONDS = 0.25  # how often those rows are looked for once the SSE has closed
SURFACE = "conversation"    # platform_context["surface"]: the prompt's results clause (the bubble has none)


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


async def _built_with(db, artifact, release) -> set[str]:
    """The agents the artifact was made with: the release's, else (a draft)
    those of the conversations editing it."""
    if release is not None:
        return set(await dashboard_chat._release_sources(db, release))
    from app.services.dashboard_collab import editing_report_ids

    reports = [str(r) for r in await editing_report_ids(db, artifact)] or [str(artifact.report_id)]
    return {str(x) for x in (await db.execute(select(report_data_source_association.c.data_source_id).where(
        report_data_source_association.c.report_id.in_(reports)))).scalars().all()}


async def allowed_agent_ids(db, artifact, release, organization, user) -> list[str]:
    """The agents this viewer may talk to through this artifact, live."""
    from app.ai.tools.implementations.agent_focus_common import accessible_agents
    from app.services.data_source_service import DataSourceService

    allowed = await _built_with(db, artifact, release) | {str(x) for x in (artifact.chat_data_source_ids or [])}
    if not allowed:
        return []
    return [str(ds.id) for ds in await accessible_agents(db, organization, user)
            if str(ds.id) in allowed and DataSourceService.is_execution_live(ds)]


async def policy(db, artifact, release, organization, user) -> dict:
    from app.models.data_source import DataSource

    agent_ids = await allowed_agent_ids(db, artifact, release, organization, user)
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
    allowed = await allowed_agent_ids(db, artifact, release, organization, user)
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
    allowed = set(await allowed_agent_ids(db, artifact, release, organization, user))
    current = await _roster(db, thread)
    keep = [a for a in current if a in allowed]
    if current and not keep:
        # Every agent it was started with is gone: it would silently become
        # data-only, which is a different conversation.
        raise AppError.forbidden(ErrorCode.CONVERSATION_AGENT_FORBIDDEN, "The agents of this conversation are no longer available to you")
    thread.model_id = dashboard_chat.chat_model_id(artifact)
    await artifact_chat_service.sync_roster(db, thread, keep)
    await db.commit()
    if release is None:  # an editor trying the draft: no published rows to hand over
        return {"source_report_title": artifact.title, "artifact_title": artifact.title,
                "scope": "agents" if keep else "data_only", "surface": SURFACE}
    return {**await dashboard_chat.build_context(db, artifact, release, keep, user), "surface": SURFACE}


def _text(blocks) -> str:
    ordered = sorted(blocks or [], key=lambda b: (b.get("block_index") or 0))
    return "\n\n".join(b["content"] for b in ordered if b.get("content"))


def _tool_title(te: dict, redaction) -> Optional[str]:
    """A tool's title as shown (the stream's ``tool`` line and history
    ``tools[]``): display-redacted; None when the redactor could not be loaded
    (``Redaction.title``)."""
    return redaction.title((te.get("arguments_json") or {}).get("title"), te.get("id"))


def _expected_steps(blocks) -> list[Optional[str]]:
    """The steps a message's successful result tools say they made (None:
    the tool names none). A tool's status does not prove its step was stored,
    so each one not found as a result means the results are incomplete."""
    from app.services.artifact_conversation_results import RESULT_TOOLS

    return [te.get("created_step_id") for te in (b.get("tool_execution") for b in blocks or [])
            if te and te.get("tool_name") in RESULT_TOOLS and te.get("status") == "success"]


def _tools(blocks, redaction) -> list[dict]:
    out = []
    for b in sorted(blocks or [], key=lambda b: (b.get("block_index") or 0)):
        te = b.get("tool_execution")
        if te:
            out.append({"name": te.get("tool_name"), "title": _tool_title(te, redaction),
                        "status": te.get("status") or b.get("status")})
    return out


async def messages(db, thread: Report, organization, user, *, limit: int, before: Optional[str]) -> dict:
    """The thread's messages, oldest first. An assistant message carries its
    tools and ``results``; ``results_incomplete: true`` (present only then)
    when they could not all be produced: the lookup failed, one was lost to
    the message budget or could not be shaped, they were withheld because
    the PII settings could not be read, or a successful result tool has no
    result (its step was not stored as a success, or is not the thread's)."""
    from app.services.artifact_conversation_results import (
        MAX_MESSAGE_RESULT_BYTES, budget_lost, fit_message, load_redaction, results_for,
    )
    from app.services.completion_service import CompletionService

    raw = await CompletionService().get_completions_v2(db, str(thread.id), organization, user, limit=limit,
                                                       before=before)
    redaction = await load_redaction(str(organization.id))
    data = raw.model_dump() if hasattr(raw, "model_dump") else dict(raw)
    items, expected = [], {}
    for c in data.get("completions") or []:
        role = "user" if c.get("role") == "user" else "assistant"
        if role == "assistant":
            expected[str(c.get("id"))] = _expected_steps(c.get("completion_blocks"))
        items.append({
            "id": str(c.get("id")), "role": role, "status": c.get("status"),
            "created_at": c["created_at"].isoformat() if hasattr(c.get("created_at"), "isoformat") else c.get("created_at"),
            "text": ((c.get("prompt") or {}).get("content") or "") if role == "user" else _text(c.get("completion_blocks")),
            **({"tools": _tools(c.get("completion_blocks"), redaction)} if role == "assistant" else {}),
        })
    assistant = [m for m in items if m["role"] == "assistant"]
    lost: set[str] = set()
    try:
        found = await results_for(db, thread_id=str(thread.id), organization_id=str(organization.id),
                                  completion_ids=[m["id"] for m in assistant], redaction=redaction, incomplete=lost)
    except Exception:
        thread_id = str(thread.id)
        logger.warning("conversation history: results lookup failed for thread %s", thread_id, exc_info=True)
        try:
            await db.rollback()  # a failed statement may leave the transaction aborted
        except Exception:
            logger.warning("conversation history: rollback after the failed results lookup failed for thread %s",
                           thread_id, exc_info=True)
        found, lost = {}, {m["id"] for m in assistant}
    for m in assistant:
        candidates = found.get(m["id"], [])
        m["results"] = fit_message(candidates, MAX_MESSAGE_RESULT_BYTES)[0]
        shown = {r["id"] for r in candidates}
        missing = any(step is None or str(step) not in shown for step in expected.get(m["id"], []))
        if m["id"] in lost or missing or budget_lost(candidates, m["results"]):
            m["results_incomplete"] = True
    items.sort(key=lambda m: m["created_at"] or "")
    return {"items": items, "next_before": data.get("next_before")}


class _Failed:
    """The SSE read raised; handled like its end (logged)."""

    def __init__(self, exc: BaseException):
        self.exc = exc


_END = object()


class _ToolLine:
    """A ``tool`` line still to send: its title is redacted on the way out."""

    def __init__(self, block_id, te: dict, status: str):
        self.block_id, self.te, self.status = block_id, te, status


class _Lookup:
    """Load the result of one tool execution of this message."""

    def __init__(self, te_id: str):
        self.te_id = te_id


class _Translator:
    """One answer's SSE frames as the SDK's events: all of ``ndjson``'s state
    and decisions, no IO (``ndjson`` pumps, looks up and redacts).

    ``frames`` parses raw SSE text; ``on_event`` turns one frame into ops:
    an event dict to send, a ``_ToolLine`` or a ``_Lookup``. ``results`` adds
    looked-up results within the message budget. ``settled`` / ``due`` say
    when ``completed`` may go (nothing pending; or the deadline passed), and
    ``completed`` builds it. Incompleteness is decided from the final state
    only, by accounting (a lookup's failure is never one by itself):
    ``incomplete`` is set by the message budget and by a final sweep that
    could only send results degraded; ``completed`` adds a result still
    pending, and a successful result tool whose step was never sent
    (``expected``, from its ``tool.finished`` or final block, else the sweep's
    result for its block: a tool's status does not prove its step was
    stored). Then ``completed`` carries ``results_incomplete: true`` (the key
    is omitted otherwise, so complete answers keep the exact old shape)."""

    def __init__(self, defer_seconds: float, thread_id: Optional[str] = None):
        from app.services.artifact_conversation_results import MAX_MESSAGE_RESULT_BYTES

        self.defer_seconds = defer_seconds
        self.thread_id = thread_id
        self.buffer = ""
        self.tools: dict = {}
        self.message_id: Optional[str] = None
        self.sent: set[str] = set()
        self.budget = MAX_MESSAGE_RESULT_BYTES
        self.pending: set[str] = set()
        self.terminal: set[str] = set()
        self.finishing = False
        self.deadline = 0.0
        self.deferred_at: Optional[float] = None
        self.first_status = "success"
        self.done = False
        self.ended = False
        self.incomplete = False
        self.expected: dict[str, Optional[str]] = {}  # result tool execution -> its step (None: not known yet)
        self.block_tool: dict[str, str] = {}  # a result tool's block id -> its tool execution

    def frames(self, chunk):
        """The events in ``chunk`` (bytes or str), lazily: none once done."""
        self.buffer += chunk.decode() if isinstance(chunk, (bytes, bytearray)) else str(chunk)
        while "\n\n" in self.buffer and not self.done:
            frame, self.buffer = self.buffer.split("\n\n", 1)
            data_line = next((x for x in frame.split("\n") if x.startswith("data: ")), None)
            if data_line is None:
                continue
            try:
                yield json.loads(data_line[6:])
            except ValueError:
                continue

    def on_event(self, evt: dict, now: float) -> list:
        from app.services.artifact_conversation_results import RESULT_TOOLS

        ops: list = []
        kind, data = evt.get("event"), evt.get("data") or {}
        if kind == "completion.started":
            self.message_id = data.get("system_completion_id")
            ops.append({"type": "started", "message_id": self.message_id, "prompt_id": evt.get("completion_id")})
        elif kind in ("tool.started", "tool.finished"):
            te_id = data.get("tool_execution_id")
            if data.get("tool_name") in RESULT_TOOLS and te_id:
                if data.get("block_id"):
                    self.block_tool[data["block_id"]] = te_id
                if kind == "tool.finished" and data.get("status") == "success":
                    self.expected[te_id] = data.get("created_step_id") or self.expected.get(te_id)
            if data.get("tool_name") in RESULT_TOOLS and te_id and te_id not in self.terminal:
                if kind == "tool.started" or data.get("status") == "success":
                    self.pending.add(te_id)  # until its final block shows the rows are stored
                else:
                    self.pending.discard(te_id)
                    self.terminal.add(te_id)
        elif kind == "block.upsert" and (data.get("block") or {}).get("tool_execution"):
            block = data["block"]
            te = block["tool_execution"]
            status = te.get("status") or block.get("status") or ""
            if self.tools.get(block.get("id")) != status:
                self.tools[block.get("id")] = status
                ops.append(_ToolLine(block.get("id"), te, status))
            if status in ("success", "error", "cancelled", "stopped") and te.get("id"):
                self.terminal.add(te["id"])
                self.pending.discard(te["id"])
            if te.get("tool_name") in RESULT_TOOLS and te.get("id") and block.get("id"):
                self.block_tool[block["id"]] = te["id"]
            if status == "success" and te.get("tool_name") in RESULT_TOOLS and te.get("id"):
                self.expected[te["id"]] = te.get("created_step_id") or self.expected.get(te["id"])
                if self.message_id and te.get("created_step_id") not in self.sent:
                    ops.append(_Lookup(te["id"]))
        elif kind == "block.delta.token" and data.get("field") == "content" and data.get("token"):
            ops.append({"type": "text_delta", "block": data.get("block_id"), "text": str(data["token"])})
        elif kind == "block.delta.text" and data.get("field") == "content" and data.get("text"):
            ops.append({"type": "text", "block": data.get("block_id"), "text": str(data["text"])})
        elif kind == "completion.finished" and not self.finishing:
            self.finishing = True
            self.first_status = data.get("status") or "success"
            self.deadline = now + self.defer_seconds
            if self.pending:
                self.deferred_at = now
                logger.info("ndjson: deferring completed; pending tool executions: %s (message %s, thread %s)",
                            sorted(self.pending), self.message_id, self.thread_id)
        elif kind == "completion.error" and not self.finishing:
            self.done = True
            ops.append({"type": "error", "code": "UNAVAILABLE",
                        "message": str(data.get("error") or data.get("message") or "The answer failed")})
        return ops

    def on_end(self, failed: Optional[BaseException]) -> Optional[dict]:
        """The SSE ended (``failed``: its read raised). Before the answer
        finished, that is an interruption."""
        if failed is not None:
            logger.warning("ndjson: the completion stream failed for message %s", self.message_id, exc_info=failed)
        self.ended = True
        if not self.finishing and not self.done:
            self.done = True
            return {"type": "error", "code": "INTERRUPTED", "message": "The answer was interrupted"}
        return None

    def learn(self, te_id: str, found: list[dict]) -> None:
        """A lookup of one tool execution found its step (its block may not name it)."""
        if found and te_id in self.expected and self.expected[te_id] is None:
            self.expected[te_id] = found[0]["id"]

    def swept(self, found: list[dict]) -> None:
        """The final sweep's results (in ``results`` already): a tool that
        named no step takes its block's result, and a pending tool whose step
        was sent is no longer pending."""
        for r in found:
            te_id = self.block_tool.get(r.get("block"))
            if te_id in self.expected and self.expected[te_id] is None:
                self.expected[te_id] = r["id"]
        self.pending = {te_id for te_id in self.pending if self.expected.get(te_id) not in self.sent}

    def results(self, found: list[dict]) -> list[bytes]:
        """Result lines for results not sent yet, within the message budget."""
        from app.services.artifact_conversation_results import budget_lost, fit_message, result_line

        out = []
        for r in found:
            if r["id"] in self.sent:
                continue
            self.sent.add(r["id"])
            kept, self.budget = fit_message([r], self.budget)
            if budget_lost([r], kept):
                self.incomplete = True
            out.extend(result_line(k) for k in kept)
        return out

    def settled(self) -> bool:
        return self.finishing and not self.done and not self.pending

    def due(self, now: float) -> bool:
        return self.finishing and not self.done and (not self.pending or now >= self.deadline)

    def completed(self, now: float) -> dict:
        if self.deferred_at is not None:
            logger.info("ndjson: deferral ended after %d ms; still pending (history only): %s (message %s, thread %s)",
                        round((now - self.deferred_at) * 1000), sorted(self.pending), self.message_id, self.thread_id)
        if self.pending or any(step is None or step not in self.sent for step in self.expected.values()):
            self.incomplete = True
        self.done = True
        return {"type": "completed", "status": self.first_status,
                **({"results_incomplete": True} if self.incomplete else {})}


async def ndjson(sse: AsyncIterator, *, thread_id: str, organization_id: str) -> AsyncIterator[bytes]:
    """The completion's SSE stream as the artifact SDK's NDJSON events:
    started, tool, text_delta, text, result, then completed (exactly once,
    last) or error. Nothing follows completed or error.

    A ``result`` is a successful create_data of this message, loaded from the
    thread's own rows (artifact_conversation_results) when its tool's final
    block arrives, or by a sweep before ``completed``. While a result tool has
    finished but its rows may not be committed yet (the agent persists them
    in the background), ``completed`` waits for them, at most
    ``RESULT_DEFER_SECONDS`` after the answer finished; if the SSE closes
    first, the rows are looked for every ``RESULT_POLL_SECONDS`` until then.
    A later one is in history only, and ``completed`` says
    ``results_incomplete: true`` (as for any result the app will not get)."""
    from app.dependencies import async_session_maker
    from app.services.artifact_conversation_results import load_redaction, results_for

    queue: asyncio.Queue = asyncio.Queue()

    async def pump():
        # The end marker always follows, even on an error or a cancel
        # (put_nowait never awaits), so the reader below never waits forever.
        try:
            async for chunk in sse:
                queue.put_nowait(chunk)
        except Exception as exc:
            queue.put_nowait(_Failed(exc))
        finally:
            queue.put_nowait(_END)

    def line(event: dict) -> bytes:
        return (json.dumps(event, default=str) + "\n").encode()

    loop = asyncio.get_running_loop()
    t = _Translator(RESULT_DEFER_SECONDS, thread_id)
    redaction = None
    lookup_warned: set[tuple] = set()  # the lookups whose failure was already warned (repeats: debug)

    async def redaction_once():
        # Loaded once per stream; a failed load (withhold) is retried when next needed.
        nonlocal redaction
        if redaction is None or not redaction.ok:
            redaction = await load_redaction(organization_id)
        return redaction

    async def lookup(**filters) -> tuple[list[dict], bool, bool]:
        """(results, lossy, failed): ``lossy`` when some could not be
        produced (withheld, unshapeable, a database error after the main
        query: the results may lack a chart); ``failed`` when it raised.
        Neither makes the answer incomplete by itself (``finish`` accounts).
        A lookup's first failure is a warning, its repeats (polls) debug."""
        lost: set[str] = set()
        try:
            async with async_session_maker() as session:
                found = await results_for(session, thread_id=thread_id, organization_id=organization_id,
                                          completion_ids=[t.message_id], redaction=await redaction_once(),
                                          incomplete=lost, **filters)
        except Exception:
            key = tuple(filters.get("tool_execution_ids") or ())
            logger.log(logging.DEBUG if key in lookup_warned else logging.WARNING,
                       "ndjson: result lookup failed for message %s (thread %s, tool executions: %s)",
                       t.message_id, thread_id, list(key) or "all (final sweep)", exc_info=True)
            lookup_warned.add(key)
            return [], False, True
        return found.get(t.message_id, []), t.message_id in lost, False

    async def tool_lookup(te_id: str) -> tuple[list[bytes], bool]:
        """One tool execution's result as lines, and whether it is settled:
        found, or it will not come now (withheld or unshapeable). A lossy
        result is not sent: the final sweep may still produce it whole (and
        decides). A failure is not settled."""
        results, lossy, failed = await lookup(tool_execution_ids=[te_id])
        t.learn(te_id, results)
        if failed:
            return [], False
        return ([] if lossy else t.results(results)), bool(results) or lossy

    async def resolve_pending() -> list[bytes]:
        out = []
        for te_id in sorted(t.pending):
            lines, settled = await tool_lookup(te_id)
            out.extend(lines)
            if settled:
                t.pending.discard(te_id)
        return out

    async def finish() -> list[bytes]:
        out = []
        if t.message_id:
            if t.pending:
                out.extend(await resolve_pending())
            results, lossy, _failed = await lookup()  # the final sweep
            sent = t.results(results)
            out.extend(sent)
            t.swept(results)
            # A failure is accounted by ``completed`` (pending, expected steps
            # not sent). A loss counts when the sweep sent something degraded
            # (no chart), or there is nothing to account against (no result
            # tool frame seen, yet some results could not be produced).
            if lossy and (sent or not t.expected):
                t.incomplete = True
        return out + [line(t.completed(loop.time()))]

    async def apply(op) -> list[bytes]:
        if isinstance(op, dict):
            return [line(op)]
        if isinstance(op, _ToolLine):
            title = (op.te.get("arguments_json") or {}).get("title")
            shown = _tool_title(op.te, await redaction_once()) if isinstance(title, str) and title else title
            return [line({"type": "tool", "id": op.block_id, "name": op.te.get("tool_name"), "title": shown,
                          "status": op.status})]
        return (await tool_lookup(op.te_id))[0]

    task = asyncio.create_task(pump())
    polling = False
    try:
        while True:
            if t.due(loop.time()):
                for out in await finish():
                    yield out
            if t.ended:
                if not (t.finishing and not t.done):
                    break
                # The SSE closed with a result pending, before the deadline:
                # its rows may still be committed (the agent's bg persist).
                if not polling:
                    polling = True
                    logger.info("ndjson: the stream ended with results pending; polling: %s (message %s, thread %s)",
                                sorted(t.pending), t.message_id, thread_id)
                for out in await resolve_pending():
                    yield out
                if t.pending:
                    await asyncio.sleep(min(RESULT_POLL_SECONDS, max(t.deadline - loop.time(), 0)))
                continue
            try:
                if t.finishing and not t.done:
                    item = await asyncio.wait_for(queue.get(), max(t.deadline - loop.time(), 0))
                else:
                    item = await queue.get()
            except asyncio.TimeoutError:
                continue
            if item is _END or isinstance(item, _Failed):
                event = t.on_end(item.exc if isinstance(item, _Failed) else None)
                if event is not None:
                    yield line(event)
                continue
            if t.done:
                continue  # read to the end, say nothing
            for evt in t.frames(item):
                for op in t.on_event(evt, loop.time()):
                    for out in await apply(op):
                        yield out
                if t.settled():
                    for out in await finish():
                        yield out
    finally:
        task.cancel()
        # The reader's own outcome (its cancel, a failure) is returned, not
        # raised; a cancellation of this stream itself still propagates.
        (outcome,) = await asyncio.gather(task, return_exceptions=True)
        if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
            logger.warning("ndjson: the completion stream reader failed for message %s (thread %s)",
                           t.message_id, thread_id, exc_info=outcome)

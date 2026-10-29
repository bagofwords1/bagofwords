"""Teams progress bubble for 1:1 chats (informative streaming).

While the agent works on a 1:1 question, Teams shows one bot bubble whose
status line updates in place ("Running a query…"), using Teams' streaming
API: a `typing` activity carrying a `streaminfo` entity opens the stream and
returns its id, later `typing` activities with that id replace the line, and
a `message` with `streamType: final` closes it.

Contract:
- `start_run` opens a bubble when a 1:1 question arrives (closing the
  previous question's bubble, so each question's bubble sits below it).
- `notify_tool_started` swaps its line as the agent starts each tool, and
  reopens a bubble when the previous one was already closed.
- Every outbound message to the conversation goes through `send_activity`,
  which turns the open bubble into that message (text, chart or file) — so a
  bubble is never left dangling above the reply, and a new one opens on the
  next tool.
- `end_run` closes a bubble nobody claimed once the run is over.

Teams rules this module follows (learn.microsoft.com, "Stream agent
messages"): one stream per chat, next request only after the previous one
returned, at most one request per second, the stream must finish within two
minutes, and the user's Stop button cancels it (403 on the next request) —
which stops the run, like the web Stop button.

State is in-process: the webhook, the background agent run and the block
senders (models/completion_block.py) all run in the same worker, and the block
senders already keep their dedupe state the same way. A restart mid-run drops
the bubble; Teams expires it on its own. Because a worker can miss the end of
a run, bubbles stop after MAX_RUN_SECONDS no matter what.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional, Set

from app.core.fire_and_forget import spawn
from app.services.teams_status_strings import (
    status_locale,
    status_text,
    tool_status_text,
)

logger = logging.getLogger(__name__)

# Teams ends a stream two minutes after its first chunk; hand over to a fresh
# bubble before that.
STREAM_MAX_SECONDS = 100.0
# Hard stop for one chat's bubbles, in case this worker never sees the run
# end (Stop handled by another worker, a run ended by a bulk UPDATE, …).
MAX_RUN_SECONDS = 15 * 60.0
# Teams throttles streaming requests at one per second.
MIN_INTERVAL_SECONDS = 1.0
# A finished run's last messages can still be on their way (background
# completions flip to success before their blocks are delivered — see
# completion_block.py), so wait this long before closing an unclaimed bubble.
END_GRACE_SECONDS = 20.0
# What TeamsAdapter.post_activity reports when a request was sent but its
# answer never came back: Teams may have accepted it, so it is not resent.
DELIVERY_UNKNOWN_STATUS = 599


class _Conversation:
    def __init__(self, adapter: Any, conversation_id: str, locale: str):
        self.adapter = adapter
        self.conversation_id = conversation_id
        self.locale = locale
        self.lock = asyncio.Lock()
        self.stream_id: Optional[str] = None
        # Which question (start_run token) the open bubble belongs to.
        self.stream_token = 0
        self.seq = 0
        self.text = ""
        self.pending_text: Optional[str] = None
        self.last_sent_at = 0.0
        self.expiry: Optional[asyncio.Task] = None
        self.open_runs = 0
        self.run_ids: Set[str] = set()
        self.ended_ids: Set[str] = set()
        # Questions are numbered; the newest one owns new bubbles.
        self.last_token = 0
        # Token of the question whose first bubble is still being opened.
        self.first_bubble_token: Optional[int] = None
        self.started_at = 0.0
        # A run in this chat ended in an error: an unclaimed bubble says so.
        self.failed = False
        # Set when the user pressed Stop or Teams refused streaming outright;
        # no more bubbles until the next question.
        self.halted = False


_conversations: Dict[str, _Conversation] = {}


def _now() -> float:
    return time.monotonic()


def _ok(response: Any) -> bool:
    return response is not None and 200 <= response.status_code < 300


def _stream_entity(conv: _Conversation, stream_type: str) -> dict:
    entity: Dict[str, Any] = {"type": "streaminfo", "streamType": stream_type}
    if conv.stream_id is not None:
        entity["streamId"] = conv.stream_id
    if stream_type != "final":
        entity["streamSequence"] = conv.seq
    return entity


async def _throttle(conv: _Conversation) -> None:
    wait = conv.last_sent_at + MIN_INTERVAL_SECONDS - _now()
    if wait > 0:
        await asyncio.sleep(wait)


def _forget_stream(conv: _Conversation) -> None:
    conv.stream_id = None
    conv.seq = 0
    conv.text = ""
    if conv.expiry is not None and conv.expiry is not asyncio.current_task():
        conv.expiry.cancel()
    conv.expiry = None


def _forget_conversation(conv: _Conversation) -> None:
    if _conversations.get(conv.conversation_id) is conv:
        del _conversations[conv.conversation_id]


def _canceled_by_user(response: Any) -> bool:
    if response is None or response.status_code != 403:
        return False
    try:
        body = response.text or ""
    except Exception:
        body = ""
    return "cancel" in body.lower()


def _on_user_stop(conv: _Conversation) -> None:
    """The user pressed Stop on the bubble: stop the runs still working."""
    logger.info("TEAMS stream: stopped by user in %s", conv.conversation_id)
    conv.halted = True
    _forget_stream(conv)
    spawn(_stop_runs(conv.run_ids - conv.ended_ids))


def _transient(response: Any) -> bool:
    """Network trouble, a lost answer or a busy Teams — not a refusal."""
    return response is None or response.status_code >= 500 or response.status_code == 429


async def _open(conv: _Conversation, text: str) -> None:
    """Open a bubble. Caller holds conv.lock."""
    await _throttle(conv)
    conv.seq = 1
    activity = {
        **conv.adapter.base_activity(conv.conversation_id),
        "type": "typing",
        "text": text,
        "entities": [_stream_entity(conv, "informative")],
    }
    response = await conv.adapter.post_activity(conv.conversation_id, activity)
    conv.last_sent_at = _now()
    stream_id = None
    if _ok(response):
        try:
            stream_id = (response.json() or {}).get("id")
        except Exception:
            stream_id = None
    if not stream_id:
        conv.seq = 0
        if _transient(response):
            logger.info("TEAMS stream: open failed for %s, will retry", conv.conversation_id)
            return
        # Streaming refused (not a 1:1 chat, feature off for this bot, …):
        # stay silent for the rest of this question rather than retry per tool.
        logger.info("TEAMS stream: open refused for %s: %s", conv.conversation_id,
                    response.status_code)
        conv.halted = True
        return
    conv.stream_id = str(stream_id)
    conv.stream_token = conv.last_token
    conv.text = text
    conv.expiry = spawn(_expire(conv, conv.stream_id, STREAM_MAX_SECONDS))


async def _update(conv: _Conversation, text: str) -> None:
    """Swap the open bubble's line. Caller holds conv.lock."""
    await _throttle(conv)
    conv.seq += 1
    activity = {
        **conv.adapter.base_activity(conv.conversation_id),
        "type": "typing",
        "text": text,
        "entities": [_stream_entity(conv, "informative")],
    }
    response = await conv.adapter.post_activity(conv.conversation_id, activity)
    conv.last_sent_at = _now()
    if _ok(response):
        conv.text = text
        return
    if _canceled_by_user(response):
        _on_user_stop(conv)
        return
    if response is not None and response.status_code == 403:
        # Expired or already completed on Teams' side: the next tool reopens.
        _forget_stream(conv)


async def _close(conv: _Conversation, text: str) -> None:
    """Close the open bubble with a plain status message. Caller holds lock."""
    await _throttle(conv)
    activity = {
        **conv.adapter.base_activity(conv.conversation_id),
        "type": "message",
        "text": text,
        "entities": [_stream_entity(conv, "final")],
    }
    response = await conv.adapter.post_activity(conv.conversation_id, activity)
    conv.last_sent_at = _now()
    if _canceled_by_user(response):
        _on_user_stop(conv)
        return
    _forget_stream(conv)


async def _expire(conv: _Conversation, stream_id: str, delay: float) -> None:
    """Hand over to a fresh bubble before Teams' two-minute limit.

    The closing bubble keeps the step it was showing (it reads as progress
    history), and a new one picks up the same line at once so the live
    indicator never goes away during a long step — up to MAX_RUN_SECONDS,
    after which the chat's bubbles stop for good.
    """
    await asyncio.sleep(delay)
    async with conv.lock:
        if conv.stream_id != stream_id:
            return
        line = conv.text or status_text(conv.locale, "still_working")
        await _close(conv, line)
        if _now() - conv.started_at >= MAX_RUN_SECONDS:
            logger.info("TEAMS stream: giving up on bubbles for %s", conv.conversation_id)
            conv.halted = True
            _forget_conversation(conv)
            return
        if not conv.halted and conv.open_runs > 0:
            await _open(conv, line)


async def _stop_runs(completion_ids: Set[str]) -> None:
    """Stop the given runs like the web Stop button."""
    from app.services.completion_service import CompletionService
    from app.settings.database import create_async_session_factory

    session_maker = create_async_session_factory()
    for completion_id in completion_ids:
        try:
            async with session_maker() as db:
                await CompletionService().update_completion_sigkill(
                    db, completion_id, None, None, authorize=False
                )
        except Exception:
            logger.exception("TEAMS stream: failed to stop run %s", completion_id)


# ── Public surface ───────────────────────────────────────────────────────


def start_run(adapter: Any, conversation_id: str, question: Optional[str],
              org_locale: Optional[str] = None) -> Optional[asyncio.Task]:
    """A 1:1 question arrived: give it a bubble below it.

    Returns at once — the bubble opens in the background so the run never
    waits on Teams. A bubble still open from an earlier question is closed
    (keeping its line) first.
    """
    if not conversation_id:
        return None
    conv = _conversations.get(conversation_id)
    if conv is None:
        conv = _conversations[conversation_id] = _Conversation(adapter, conversation_id, "en")
    conv.adapter = adapter
    conv.locale = status_locale(question, org_locale)
    conv.open_runs += 1
    conv.halted = False
    conv.failed = False
    conv.started_at = _now()
    conv.last_token += 1
    conv.first_bubble_token = conv.last_token
    return spawn(_open_first_bubble(conv, conv.last_token))


async def _open_first_bubble(conv: _Conversation, token: int) -> None:
    try:
        async with conv.lock:
            # Skip when the run already finished, a reply already went out, a
            # newer question took over, or this question's own tool opened
            # the bubble first — a bubble must never land below an answer.
            if (_conversations.get(conv.conversation_id) is not conv or conv.open_runs <= 0
                    or conv.first_bubble_token != token or conv.halted):
                return
            if conv.stream_id is not None:
                if conv.stream_token == token:
                    return
                await _close(conv, conv.text or status_text(conv.locale, "still_working"))
                # A Stop seen on that close belongs to the earlier question
                # (its runs are already being stopped); this question still
                # gets its bubble.
                conv.halted = False
            await _open(conv, status_text(conv.locale, "working"))
    except Exception:
        logger.exception("TEAMS stream: failed to open bubble")
    finally:
        if conv.first_bubble_token == token:
            conv.first_bubble_token = None


def abort_run(conversation_id: Optional[str]) -> None:
    """The question never became a run (completion creation failed)."""
    conv = _conversations.get(conversation_id or "")
    if conv is not None:
        conv.open_runs = max(0, conv.open_runs - 1)
        spawn(_finish(conv, delay=0.0))


def notify_tool_started(conversation_id: Optional[str], completion_id: Optional[str],
                        tool_name: Optional[str], title: Optional[str] = None) -> Optional[asyncio.Task]:
    """The agent started a tool: show it on the bubble. Never blocks the agent.

    `title` is what the tool works on (a query or dashboard title the agent
    gave it), shown after the tool's line.
    """
    conv = _conversations.get(conversation_id or "")
    if conv is None or conv.halted:
        return None
    if completion_id and completion_id not in conv.ended_ids:
        conv.run_ids.add(completion_id)
    conv.pending_text = tool_status_text(conv.locale, tool_name, title)
    return spawn(_apply_pending(conv))


async def _apply_pending(conv: _Conversation) -> None:
    try:
        async with conv.lock:
            # Several tools can start in a burst; the throttle wait lets the
            # newest line win, and later waiters find nothing left to send.
            await _throttle(conv)
            text, conv.pending_text = conv.pending_text, None
            if text is None or conv.halted or conv.open_runs <= 0:
                return
            if conv.stream_id is None:
                await _open(conv, text)
            elif text != conv.text:
                await _update(conv, text)
    except Exception:
        logger.exception("TEAMS stream: failed to update bubble")


async def send_activity(adapter: Any, conversation_id: str, activity: dict) -> bool:
    """Send an outbound message; if a bubble is open, the message becomes it."""
    conv = _conversations.get(conversation_id or "")
    if conv is None:
        return _ok(await adapter.post_activity(conversation_id, activity))
    async with conv.lock:
        if conv.stream_id is None:
            # A reply beat the first bubble out: don't open one below it.
            conv.first_bubble_token = None
            response = await adapter.post_activity(conversation_id, activity)
            conv.last_sent_at = _now()
            return _ok(response)
        await _throttle(conv)
        final = {
            **activity,
            "entities": [*(activity.get("entities") or []), _stream_entity(conv, "final")],
        }
        response = await adapter.post_activity(conversation_id, final)
        conv.last_sent_at = _now()
        if _ok(response):
            _forget_stream(conv)
            return True
        if _canceled_by_user(response):
            # The user stopped this reply: treat it as discarded.
            _on_user_stop(conv)
            return False
        if response is not None and response.status_code == DELIVERY_UNKNOWN_STATUS:
            # Sent, answer lost: Teams may already show it — resending risks a
            # duplicate reply.
            logger.warning("TEAMS stream: delivery of a reply unknown in %s", conversation_id)
            _forget_stream(conv)
            return False
        # Rejected (or never reached Teams). Close the bubble with its own
        # line when Teams is reachable, so it doesn't keep spinning above the
        # reply, then send the reply on its own so it is never lost.
        if response is not None:
            await _close(conv, conv.text or status_text(conv.locale, "still_working"))
            if conv.halted:
                return False
        _forget_stream(conv)
        response = await adapter.post_activity(conversation_id, activity)
        conv.last_sent_at = _now()
        return _ok(response)


async def end_run(conversation_id: Optional[str], completion_id: Optional[str] = None,
                  failed: bool = False) -> None:
    """A run in this chat finished (success, error or stopped).

    `failed` closes a bubble nobody claimed with an error line instead of
    "Done". A run reported twice (status listener and the run's own finally)
    counts once.
    """
    conv = _conversations.get(conversation_id or "")
    if conv is None:
        return
    if completion_id:
        if completion_id in conv.ended_ids:
            return
        conv.ended_ids.add(completion_id)
        conv.run_ids.discard(completion_id)
    conv.open_runs = max(0, conv.open_runs - 1)
    if failed:
        conv.failed = True
    await _finish(conv, delay=END_GRACE_SECONDS)


async def _finish(conv: _Conversation, delay: float) -> None:
    if delay:
        await asyncio.sleep(delay)
    if conv.open_runs > 0:
        return
    async with conv.lock:
        if conv.open_runs > 0:
            return
        if conv.stream_id is not None:
            await _close(conv, status_text(conv.locale, "failed" if conv.failed else "done"))
        _forget_conversation(conv)

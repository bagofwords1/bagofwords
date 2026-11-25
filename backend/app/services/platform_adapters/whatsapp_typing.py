"""WhatsApp "typing…" indicator while the agent works on a question.

WhatsApp shows "typing…" under the bot's name (and a ••• bubble in the chat)
when the Cloud API is sent a read receipt carrying ``typing_indicator`` for a
received message. Meta dismisses it when the bot sends a message, or after 25
seconds — a run usually takes longer, so it is re-sent every
``REFRESH_SECONDS`` (timed from each send) until the run ends. It sits on top
of the 👀 → ✅ reaction, which stays as is: if WhatsApp refuses the indicator
(a 4xx), the loop stops and the reaction still shows the bot is working. A
transient failure (network, timeout, 429, 5xx) just waits for the next
refresh.

A chat is one user (wa_id) on one business number (phone_number_id): the same
person messaging two of the org's numbers has two independent chats.

Runs are tracked one by one. ``start_run`` returns a token; once the system
completion exists the caller ``bind_run``s it to the completion id, and only
an end reported for that id (``end_run``) ends the run. Ends for ids this
module never bound — e.g. a stale row from before a restart being retired —
are ignored, so they can't stop a live run's indicator. Each run's end is
reported twice (the run's own ``finally`` and the completion status listener)
and counted once; an end that arrives before the bind is remembered and
applied when the bind comes. The indicator stops once every run in the chat
has ended, and the loop gives up after ``MAX_RUN_SECONDS`` whatever happens.

Every message the bot sends dismisses the indicator on WhatsApp, so after one
the indicator is re-sent ``AFTER_SEND_GRACE_SECONDS`` later instead of waiting
for the next refresh — a mid-run message (a file, a chart, a partial answer)
doesn't leave the chat without "typing…" for up to 20 seconds. The short wait
is what keeps it from reappearing under the final answer: the run ends within
it and the pending send is cancelled.

A chat with no open run is dropped, adapter (and its decrypted credentials)
included.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, Optional, Set, Tuple

from app.core.fire_and_forget import spawn

logger = logging.getLogger(__name__)

# Meta drops the indicator after 25s; refresh a little before that.
REFRESH_SECONDS = 20.0
# Hard cap per chat, so a run whose end is never reported can't keep the
# indicator up forever.
MAX_RUN_SECONDS = 15 * 60.0
# After the bot sends a message to the chat, show "typing…" again this much
# later (unless the run ended meanwhile).
AFTER_SEND_GRACE_SECONDS = 5.0
# How many completion ids are remembered for de-duplication / early ends.
_IDS_KEPT = 500

# send_typing_indicator outcomes
SENT = "sent"
REFUSED = "refused"      # WhatsApp said no (4xx): stop for this run
TRANSIENT = "transient"  # network / timeout / 429 / 5xx: try again next refresh

ChatKey = Tuple[str, str]  # (phone_number_id, wa_id)


def _now() -> float:
    return time.monotonic()


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class _Chat:
    def __init__(self, key: ChatKey, adapter: Any):
        self.key = key
        self.adapter = adapter
        self.message_id: Optional[str] = None
        self.runs: Set[str] = set()
        self.started_at = 0.0
        # Why the loop stopped by itself ("refused" / "deadline"); a message
        # sent afterwards must not restart it.
        self.stopped: Optional[str] = None
        self.task: Optional[asyncio.Task] = None


class _Run:
    def __init__(self, key: ChatKey):
        self.key = key
        self.completion_id: Optional[str] = None
        # Bound to no completion id (the caller couldn't tell it): any later
        # end reported for this wa_id ends it — the pre-binding behaviour.
        self.accepts_any_end = False


_chats: Dict[ChatKey, _Chat] = {}
_runs: Dict[str, _Run] = {}
_bound: Dict[str, str] = {}                                 # completion id -> run token
_ended_ids: "OrderedDict[str, None]" = OrderedDict()        # ends already applied
_early_ends: "OrderedDict[str, None]" = OrderedDict()       # ends for ids not bound (yet)


def _remember(ids: "OrderedDict[str, None]", completion_id: str) -> None:
    ids[completion_id] = None
    ids.move_to_end(completion_id)
    while len(ids) > _IDS_KEPT:
        ids.popitem(last=False)


def _cancel(chat: _Chat) -> None:
    if chat.task is not None and not chat.task.done():
        chat.task.cancel()
    chat.task = None


def _restart(chat: _Chat, first_delay: float = 0.0) -> None:
    _cancel(chat)
    chat.task = spawn(_keep_typing(chat, first_delay))


async def _keep_typing(chat: _Chat, first_delay: float = 0.0) -> None:
    deadline = chat.started_at + MAX_RUN_SECONDS
    try:
        if first_delay > 0:
            await _sleep(first_delay)
        while True:
            if _now() >= deadline:
                chat.stopped = "deadline"
                return
            sent_at = _now()
            outcome = await chat.adapter.send_typing_indicator(chat.message_id)
            if outcome == REFUSED:
                # WhatsApp refused it (API version, expired message id).
                # Don't keep hammering it — the 👀 still shows.
                chat.stopped = "refused"
                logger.info("WhatsApp typing indicator refused; stopping for this run")
                return
            # Timed from the send, so a slow Graph round trip doesn't push
            # refreshes past Meta's 25s.
            await _sleep(max(0.0, sent_at + REFRESH_SECONDS - _now()))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("WhatsApp typing indicator loop failed")


def _finish_run(token: str) -> None:
    run = _runs.pop(token, None)
    if run is None:
        return
    if run.completion_id:
        _bound.pop(run.completion_id, None)
    chat = _chats.get(run.key)
    if chat is None:
        return
    chat.runs.discard(token)
    if not chat.runs:
        _cancel(chat)
        del _chats[run.key]


# ── Public surface ───────────────────────────────────────────────────────


def start_run(adapter: Any, phone_number_id: Optional[str], chat_id: Optional[str],
              message_id: Optional[str]) -> Optional[str]:
    """A question arrived: show "typing…" on it until its run ends.

    Returns the run's token (None when there's nothing to show it on).
    Returns at once; sending happens in the background.
    """
    if not chat_id or not message_id:
        return None
    key: ChatKey = (str(phone_number_id or ""), str(chat_id))
    chat = _chats.get(key)
    if chat is None:
        chat = _chats[key] = _Chat(key, adapter)
    chat.adapter = adapter
    chat.message_id = message_id
    chat.started_at = _now()
    chat.stopped = None
    token = uuid.uuid4().hex
    _runs[token] = _Run(key)
    chat.runs.add(token)
    # (Re)start so the newest message gets the indicator right away.
    _restart(chat)
    return token


def bind_run(token: Optional[str], completion_id: Optional[str]) -> None:
    """The run's system completion exists: only its end ends the run.

    Without an id the run falls back to ending on any end reported for its
    wa_id from now on.
    """
    run = _runs.get(token or "")
    if run is None:
        return
    if not completion_id:
        run.accepts_any_end = True
        return
    completion_id = str(completion_id)
    if completion_id in _early_ends or completion_id in _ended_ids:
        # The run already ended before we could bind it.
        _early_ends.pop(completion_id, None)
        _remember(_ended_ids, completion_id)
        _finish_run(token)
        return
    run.completion_id = completion_id
    _bound[completion_id] = token


def abort_run(token: Optional[str]) -> None:
    """The run for this token was never created."""
    if token:
        _finish_run(token)


def end_run(chat_id: Optional[str], completion_id: Optional[str] = None) -> None:
    """A run finished, failed or was stopped.

    Reported more than once per run; each completion id counts once. An id
    that was never bound here is remembered (its bind may still come) and
    otherwise ignored.
    """
    if not completion_id:
        return
    completion_id = str(completion_id)
    if completion_id in _ended_ids:
        return
    token = _bound.get(completion_id)
    if token is None:
        legacy = [t for t, r in _runs.items() if r.accepts_any_end and r.key[1] == str(chat_id or "")]
        if not legacy:
            _remember(_early_ends, completion_id)
            return
        token = legacy[0]
    _remember(_ended_ids, completion_id)
    _finish_run(token)


def note_outbound(phone_number_id: Optional[str], chat_id: Optional[str]) -> None:
    """The bot just sent a message (answer, file, image) to this chat.

    That dismissed "typing…" on WhatsApp; bring it back shortly if a run is
    still going.
    """
    chat = _chats.get((str(phone_number_id or ""), str(chat_id or "")))
    if chat is None or not chat.runs or chat.stopped:
        return
    _restart(chat, first_delay=AFTER_SEND_GRACE_SECONDS)

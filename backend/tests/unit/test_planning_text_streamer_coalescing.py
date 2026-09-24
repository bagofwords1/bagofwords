"""Planner text streaming coalesces token deltas without changing the text
the client reconstructs (tokens appended, snapshots replace)."""
import asyncio

import pytest

from app.streaming.text_streamer import PlanningTextStreamer


def _make(**kw):
    events = []

    async def emit(event):
        events.append(event)

    async def seq():
        return len(events) + 1

    s = PlanningTextStreamer(emit=emit, seq_fn=seq, completion_id="c", agent_execution_id="e", block_id="b", **kw)
    return s, events


def _client_text(events, field):
    """What the chat bubble shows: tokens append, snapshots replace."""
    text = ""
    for e in events:
        if e.data.get("field") != field:
            continue
        if e.event == "block.delta.token":
            text += e.data["token"]
        elif e.event == "block.delta.text":
            text = e.data["text"]
    return text


def _chunks(text, size):
    return [text[: i + size] for i in range(0, len(text), size)]


@pytest.mark.asyncio
async def test_fast_stream_is_coalesced_and_reconstructs_exactly():
    reasoning = "Looking at the invoice table to total sales per year. " * 8
    content = "I'll build a yearly revenue chart from Invoice.Total." * 3
    s, events = _make()
    for r in _chunks(reasoning, 3):
        await s.update(r, "")
    for c in _chunks(content, 3):
        await s.update(reasoning, c)
    tokens_before_complete = sum(e.event == "block.delta.token" for e in events)
    await s.complete()

    assert _client_text(events, "reasoning") == reasoning
    assert _client_text(events, "content") == content
    # One event per LLM chunk before; now bounded by char_threshold batches.
    n_updates = len(_chunks(reasoning, 3)) + len(_chunks(content, 3))
    assert tokens_before_complete < n_updates / 5
    # Tokens only carry text past the last snapshot, in order.
    assert [e.seq for e in events] == sorted(e.seq for e in events)


@pytest.mark.asyncio
async def test_quiet_tail_is_flushed_by_the_timer():
    s, events = _make()
    await s.update("Thinking", "")  # first delta goes out immediately
    assert [e.data.get("token") for e in events if e.event == "block.delta.token"] == ["Thinking"]
    await s.update("Thinking about", "")  # under threshold, inside the throttle window
    await asyncio.sleep(0.05)
    assert _client_text(events, "reasoning") == "Thinking about"


@pytest.mark.asyncio
async def test_source_switch_flushes_pending_before_replacement():
    s, events = _make(throttle_ms=10_000)
    await s.update("", "Running the query")
    await s.update("", "Running the query now")  # pending " now"
    await s.update("", "Revenue grew 12%", reset_on_source_change=True)
    await s.complete()
    replace = [i for i, e in enumerate(events) if e.data.get("replace")]
    last_token = max(i for i, e in enumerate(events) if e.event == "block.delta.token")
    assert replace and last_token < replace[0]
    assert _client_text(events, "content") == "Revenue grew 12%"


@pytest.mark.parametrize("prev, new, delta", [
    ("abc", "abcdef", "def"), ("", "x", "x"), ("abc", "abX", "X"), ("abc", "abc", ""),
])
def test_delta(prev, new, delta):
    assert PlanningTextStreamer._delta(prev, new) == delta

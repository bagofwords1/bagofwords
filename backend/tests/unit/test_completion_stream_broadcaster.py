"""Unit tests for the CompletionEventQueue broadcaster and live-stream registry.

These back the SSE reconnect feature: a client that lost its kickoff stream
(page refresh, network drop) re-attaches via the watch endpoint, which
subscribes to the live queue when this worker owns the run. The contracts:

  1. Fan-out — every subscriber receives events put after it subscribed;
     the primary (kickoff) consumer always receives everything.
  2. Termination — finish() ends every consumer, including ones that
     subscribe after the producer finished (no hung streams).
  3. Non-blocking producer — a full/dead subscriber queue drops events
     rather than stalling the agent, and does not affect other consumers.
  4. Heartbeat — next_event() returns HEARTBEAT on timeout so stream
     generators can emit keep-alive comments on quiet stretches.
  5. Registry — active streams are discoverable by completion id while the
     run lives, and gone once unregistered.
"""

import asyncio

import pytest

from app.schemas.sse_schema import SSEEvent
from app.streaming.completion_stream import (
    CompletionEventQueue,
    HEARTBEAT,
    STREAM_DONE,
    get_active_stream,
    register_stream,
    unregister_stream,
)


def _event(name: str) -> SSEEvent:
    return SSEEvent(event=name, completion_id="c1", data={})


@pytest.mark.asyncio
async def test_subscriber_receives_events_and_stream_done():
    q = CompletionEventQueue()
    await q.put(_event("before.subscribe"))

    sub = q.subscribe()
    await q.put(_event("after.subscribe"))
    q.finish()

    item = await CompletionEventQueue.next_event(sub, timeout=1)
    assert isinstance(item, SSEEvent) and item.event == "after.subscribe"
    assert await CompletionEventQueue.next_event(sub, timeout=1) is STREAM_DONE


@pytest.mark.asyncio
async def test_primary_consumer_receives_all_events():
    q = CompletionEventQueue()
    await q.put(_event("one"))
    await q.put(_event("two"))
    q.finish()

    received = [e.event async for e in q.get_events()]
    assert received == ["one", "two"]


@pytest.mark.asyncio
async def test_late_subscriber_after_finish_terminates_immediately():
    q = CompletionEventQueue()
    q.finish()
    sub = q.subscribe()
    assert await CompletionEventQueue.next_event(sub, timeout=1) is STREAM_DONE


@pytest.mark.asyncio
async def test_multiple_subscribers_each_get_events():
    q = CompletionEventQueue()
    subs = [q.subscribe() for _ in range(3)]
    await q.put(_event("fan.out"))
    for sub in subs:
        item = await CompletionEventQueue.next_event(sub, timeout=1)
        assert isinstance(item, SSEEvent) and item.event == "fan.out"


@pytest.mark.asyncio
async def test_full_subscriber_drops_without_blocking_others():
    q = CompletionEventQueue()
    dead = q.subscribe()
    live = q.subscribe()

    # Overflow the dead subscriber's bounded queue.
    for i in range(600):
        await q.put(_event(f"e{i}"))

    # The producer never blocked, and the live subscriber still terminates
    # (its sentinel lands after a drain if needed).
    q.finish()
    assert dead.qsize() > 0  # partially filled, rest dropped
    # Drain the live subscriber to its sentinel.
    saw_done = False
    for _ in range(700):
        item = await CompletionEventQueue.next_event(live, timeout=1)
        if item is STREAM_DONE:
            saw_done = True
            break
    assert saw_done


@pytest.mark.asyncio
async def test_subscriber_receives_running_tool_replay():
    """Tool executions are write-on-complete (no DB row until they finish), so
    a resuming client can only learn about an in-flight tool from the live
    queue: subscribe() must replay tool.started for tools still running, and
    must not replay tools that already finished."""
    q = CompletionEventQueue()
    await q.put(SSEEvent(event="tool.started", completion_id="c1",
                         data={"tool_execution_id": "te-running", "tool_name": "create_data"}))
    await q.put(SSEEvent(event="tool.started", completion_id="c1",
                         data={"tool_execution_id": "te-done", "tool_name": "inspect_data"}))
    await q.put(SSEEvent(event="tool.finished", completion_id="c1",
                         data={"tool_execution_id": "te-done", "status": "success"}))

    sub = q.subscribe()
    replayed = await CompletionEventQueue.next_event(sub, timeout=1)
    assert isinstance(replayed, SSEEvent) and replayed.event == "tool.started"
    assert replayed.data["tool_execution_id"] == "te-running"
    # Nothing else pending: the finished tool was not replayed.
    assert await CompletionEventQueue.next_event(sub, timeout=0.05) is HEARTBEAT


@pytest.mark.asyncio
async def test_next_event_heartbeat_on_timeout():
    q = CompletionEventQueue()
    sub = q.subscribe()
    assert await CompletionEventQueue.next_event(sub, timeout=0.05) is HEARTBEAT


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery():
    q = CompletionEventQueue()
    sub = q.subscribe()
    q.unsubscribe(sub)
    await q.put(_event("gone"))
    assert sub.qsize() == 0


def test_registry_lifecycle():
    q = CompletionEventQueue()
    register_stream("comp-1", q)
    assert get_active_stream("comp-1") is q
    unregister_stream("comp-1")
    assert get_active_stream("comp-1") is None
    # Unregistering twice is harmless
    unregister_stream("comp-1")

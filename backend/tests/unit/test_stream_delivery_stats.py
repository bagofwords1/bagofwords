"""Unit tests for app.streaming.delivery: the per-stream stats line and the
buffered-delivery verdict."""
import asyncio
import logging

import pytest

from app.streaming.delivery import instrument_stream, is_buffered


async def _events(n: int, completion_id: str):
    yield f'event: completion.started\ndata: {{"data": {{"system_completion_id": "{completion_id}"}}}}\n\n'
    for i in range(n):
        yield f'event: block.delta.token\ndata: {{"i": {i}}}\n\n'
    yield ": ping\n\n"


@pytest.fixture
def delivery_log(caplog, monkeypatch):
    """Capture app.streaming.delivery even if app logging setup disabled it."""
    lg = logging.getLogger("app.streaming.delivery")
    monkeypatch.setattr(lg, "disabled", False)
    monkeypatch.setattr(lg, "handlers", [caplog.handler])
    monkeypatch.setattr(lg, "propagate", False)
    caplog.set_level(logging.INFO, logger=lg.name)
    return caplog


def _stats_line(caplog):
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("sse.stream ")]
    assert len(lines) == 1, lines
    return dict(part.split("=", 1) for part in lines[0].split() if "=" in part)


@pytest.mark.parametrize("n", [0, 3, 250])
def test_stats_count_events_and_bytes_and_name_the_run(n, delivery_log):
    cid = "7f9c1d2e-0000-4000-8000-1234567890ab"

    async def run():
        chunks = []
        async for c in instrument_stream(_events(n, cid), "kickoff"):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(run())

    stats = _stats_line(delivery_log)
    assert stats["outcome"] == "finished"
    assert stats["completion"] == cid
    assert int(stats["events"]) == n + 1  # heartbeat comments are not events
    assert int(stats["bytes"]) == sum(len(c.encode()) for c in chunks)
    assert stats["first_byte_ms"] != "-"


def test_stats_record_a_disconnect(delivery_log):
    async def run():
        gen = instrument_stream(_events(50, "c-1-2-3-4-5-6-7"), "watch", completion_id="given-id")
        await gen.__anext__()
        await gen.aclose()  # the client went away after the first event

    asyncio.run(run())

    stats = _stats_line(delivery_log)
    assert stats["outcome"] == "disconnected"
    assert stats["completion"] == "given-id"
    assert int(stats["events"]) == 1


def test_disconnect_closes_the_wrapped_stream_immediately(delivery_log):
    """The wrapped generator's cleanup (e.g. unsubscribing from the live queue)
    must run when the client goes away, not whenever it is garbage-collected."""
    cleaned_up = []

    async def source():
        try:
            for i in range(100):
                yield f"event: block.delta.token\ndata: {i}\n\n"
        finally:
            cleaned_up.append(True)

    async def run():
        gen = instrument_stream(source(), "watch", completion_id="c")
        await gen.__anext__()
        await gen.aclose()
        return list(cleaned_up)

    assert asyncio.run(run()) == [True]


@pytest.mark.parametrize("holdback,span,expected", [
    (0, 10_000, False),         # live
    (1_499, 2_000, False),      # below the absolute floor
    (1_500, 3_000, True),       # held back half the turn
    (2_000, 10_000, False),     # a hiccup, not a buffered answer
    (3_200, 9_600, True),       # released in a few large chunks
    (9_900, 10_000, True),      # the whole answer arrived at the end
    (60_000, 60_500, True),
])
def test_buffered_verdict(holdback, span, expected):
    assert is_buffered(holdback, span) is expected

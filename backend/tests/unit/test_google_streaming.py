"""Gemini streams must reach the caller chunk by chunk, as they arrive.

google-genai's stream is a blocking generator. The client used to read the
whole response before passing anything on (``inference_stream_v2``), or read it
on the event loop thread (``inference_stream``), which froze every other
request while Gemini was replying. The fake stream below holds back its tail
until the test releases it, so these tests fail if either problem comes back.
"""
import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.llm.clients.google_client import Google  # noqa: E402
from app.ai.llm.types import Message, MessageStopEvent, TextDeltaEvent  # noqa: E402

# Upper bound only, so a broken client fails instead of hanging the suite.
WAIT = 5


def _chunk(text):
    part = SimpleNamespace(text=text, thought=False, function_call=None)
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]), finish_reason=None)],
        usage_metadata=SimpleNamespace(prompt_token_count=7, candidates_token_count=3),
    )


class _GatedStream:
    """Yields ``head`` right away, then waits for ``release`` before the rest."""

    def __init__(self, head, tail, fail=None):
        self.head, self.tail, self.fail = head, tail, fail
        self.release = threading.Event()
        self.finished = threading.Event()
        self.ended = threading.Event()
        self.released_in_time = None

    def generate_content_stream(self, **_):
        try:
            yield from map(_chunk, self.head)
            self.released_in_time = self.release.wait(WAIT)
            if self.fail:
                raise self.fail
            yield from map(_chunk, self.tail)
            self.finished.set()
        finally:
            self.ended.set()


def _client(stream):
    client = Google(api_key="test-key")
    client.client = SimpleNamespace(models=stream)
    return client


def _v2(client):
    return client.inference_stream_v2("gemini-2.5-flash", [Message(role="user", content="hi")])


def _v1(client):
    return client.inference_stream("gemini-2.5-flash", "hi")


def _text(item):
    return item.text if isinstance(item, TextDeltaEvent) else item if isinstance(item, str) else None


async def _release_from_event_loop(stream):
    # Runs only if the event loop is free while the client reads the stream.
    await asyncio.sleep(0)
    stream.release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("open_stream", [_v1, _v2], ids=["inference_stream", "inference_stream_v2"])
@pytest.mark.parametrize("head,tail", [(["Hel"], ["lo", " world"]), (["a", "b"], ["c"])])
async def test_first_text_arrives_before_the_response_ends(open_stream, head, tail):
    stream = _GatedStream(head, tail)
    gen = open_stream(_client(stream))

    first = None
    while first is None:
        first = _text(await asyncio.wait_for(gen.__anext__(), WAIT))

    assert first == head[0]
    assert not stream.finished.is_set(), "text was held back until Gemini finished"

    # The rest must still arrive, and only thanks to a coroutine on the same
    # event loop releasing it — proving the loop isn't blocked by the read.
    releaser = asyncio.create_task(_release_from_event_loop(stream))
    rest = [item async for item in gen]
    await releaser

    assert stream.released_in_time, "event loop was blocked while reading Gemini"
    assert [first] + [t for t in map(_text, rest) if t] == head + tail
    if open_stream is _v2:
        assert isinstance(next(e for e in rest if not _text(e)), MessageStopEvent)


@pytest.mark.asyncio
@pytest.mark.parametrize("open_stream", [_v1, _v2], ids=["inference_stream", "inference_stream_v2"])
async def test_usage_is_recorded_after_streaming(open_stream):
    stream = _GatedStream(["x"], ["y"])
    stream.release.set()
    client = _client(stream)
    [_ async for _ in open_stream(client)]
    usage = client.pop_last_usage()
    assert usage.prompt_tokens == 7 and usage.completion_tokens == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("open_stream", [_v1, _v2], ids=["inference_stream", "inference_stream_v2"])
async def test_mid_stream_errors_reach_the_caller(open_stream):
    stream = _GatedStream(["partial"], [], fail=RuntimeError("quota exceeded"))
    stream.release.set()
    with pytest.raises(RuntimeError, match="quota"):
        [_ async for _ in open_stream(_client(stream))]


@pytest.mark.asyncio
async def test_closing_the_stream_early_stops_reading_from_gemini():
    stream = _GatedStream(["one"], ["two", "three", "four"])
    gen = _v2(_client(stream))
    while _text(await asyncio.wait_for(gen.__anext__(), WAIT)) is None:
        pass
    await gen.aclose()
    stream.release.set()

    assert await asyncio.to_thread(stream.ended.wait, WAIT), "Gemini stream left open"
    assert not stream.finished.is_set(), "kept reading Gemini after the caller stopped"

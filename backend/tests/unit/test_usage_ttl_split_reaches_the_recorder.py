"""The TTL split a provider reports must survive all the way to the cost row.

A 1-hour cache write bills at 2x where a 5-minute one bills at 1.25x. The
provider reports which it charged, but that only helps if the number survives
the hop from the client's UsageEvent, through the LLM wrapper's accumulators,
into LLMUsageRecorderService. Any link dropping it silently reverts the bill to
the cheaper rate, which looks exactly like normal operation.

This drives the real client and the real wrapper with only the provider's HTTP
call stubbed, and asserts on what the recorder is handed.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest  # noqa: E402

from app.ai.llm.clients.anthropic_client import Anthropic  # noqa: E402
from app.ai.llm.types import Message, UsageEvent  # noqa: E402

WRITE = 38_902


class _Usage:
    """Mirrors the SDK's Usage: cache_creation rides alongside the total."""

    input_tokens = 2_999
    output_tokens = 510
    cache_read_input_tokens = 0
    cache_creation_input_tokens = WRITE

    def __init__(self, five_m: int, one_h: int):
        self.cache_creation = {
            "ephemeral_5m_input_tokens": five_m,
            "ephemeral_1h_input_tokens": one_h,
        }


class _Chunk:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


async def _usage_event(usage_obj) -> UsageEvent:
    client = Anthropic(api_key="test-key-not-used")

    async def _fake_create(**_kwargs):
        return _Stream([_Chunk("message_start", message=type("M", (), {"usage": usage_obj})())])

    client.async_client.messages.create = _fake_create
    events = [
        e async for e in client.inference_stream_v2(
            model_id="claude-haiku-4-5-20251001",
            messages=[Message(role="user", content="hi")],
            system="stable half",
        )
    ]
    usage_events = [e for e in events if isinstance(e, UsageEvent)]
    assert usage_events, "the stream must always end with a usage event"
    return usage_events[-1]


@pytest.mark.asyncio
async def test_one_hour_writes_are_reported_as_one_hour():
    event = await _usage_event(_Usage(five_m=0, one_h=WRITE))

    assert event.cache_creation_tokens == WRITE
    assert event.cache_write_1h_tokens == WRITE
    assert event.cache_write_5m_tokens == 0


@pytest.mark.asyncio
async def test_five_minute_writes_are_reported_as_five_minute():
    event = await _usage_event(_Usage(five_m=WRITE, one_h=0))

    assert event.cache_write_5m_tokens == WRITE
    assert event.cache_write_1h_tokens == 0


@pytest.mark.asyncio
async def test_a_mixed_request_keeps_both_buckets():
    """Breakpoints may carry different TTLs in one request, so the two are not
    mutually exclusive and neither may swallow the other."""
    event = await _usage_event(_Usage(five_m=1_000, one_h=2_000))

    assert (event.cache_write_5m_tokens, event.cache_write_1h_tokens) == (1_000, 2_000)


@pytest.mark.asyncio
async def test_a_provider_that_omits_the_split_falls_back_to_the_cheaper_rate():
    """Never over-bill on missing data."""

    class _NoSplit:
        input_tokens = 10
        output_tokens = 5
        cache_read_input_tokens = 0
        cache_creation_input_tokens = WRITE
        cache_creation = None

    event = await _usage_event(_NoSplit())

    assert event.cache_creation_tokens == WRITE
    assert event.cache_write_5m_tokens == WRITE
    assert event.cache_write_1h_tokens == 0


def test_the_recorder_bills_the_two_ttls_differently():
    """The hop's whole purpose: a 1-hour write must cost more than a 5-minute
    one of the same size. If the recorder ignored the split these would match."""
    from app.services.llm_usage_recorder import LLMUsageRecorderService

    class _Model:
        model_id = "claude-haiku-4-5-20251001"

        def get_input_cost_rate(self):
            return 1.0

    five = LLMUsageRecorderService._calc_input_cost(
        _Model(), 0, 0, WRITE, "anthropic", cache_write_5m_tokens=WRITE, cache_write_1h_tokens=0,
    )
    hour = LLMUsageRecorderService._calc_input_cost(
        _Model(), 0, 0, WRITE, "anthropic", cache_write_5m_tokens=0, cache_write_1h_tokens=WRITE,
    )
    assert hour > five
    assert hour == pytest.approx(five * (2.00 / 1.25))


def test_an_unsplit_write_still_bills_at_the_five_minute_rate():
    """Rows written before the split existed keep their original price."""
    from app.services.llm_usage_recorder import LLMUsageRecorderService

    class _Model:
        model_id = "claude-haiku-4-5-20251001"

        def get_input_cost_rate(self):
            return 1.0

    legacy = LLMUsageRecorderService._calc_input_cost(_Model(), 0, 0, WRITE, "anthropic")
    explicit = LLMUsageRecorderService._calc_input_cost(
        _Model(), 0, 0, WRITE, "anthropic", cache_write_5m_tokens=WRITE,
    )
    assert legacy == pytest.approx(explicit)

@pytest.mark.asyncio
async def test_the_wrapper_forwards_the_split_to_the_recorder(monkeypatch):
    """Closes the middle hop: client -> LLM.inference_stream_v2 accumulators ->
    LLMUsageRecorderService. Each link is individually plausible and silently
    reverts the bill to the cheaper rate if it drops the field, so the whole
    chain is asserted rather than its ends."""
    from app.ai.llm import llm as llm_mod

    captured = {}

    def _capture(self, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(llm_mod.LLM, "_schedule_usage_record", _capture)

    # Build the wrapper without touching provider construction: only the
    # attributes the streaming path reads are needed, and stubbing them keeps
    # the accumulators under test rather than the constructor.
    wrapper = llm_mod.LLM.__new__(llm_mod.LLM)
    wrapper.client = Anthropic(api_key="test-key-not-used")
    wrapper.model_id = "claude-haiku-4-5-20251001"
    wrapper.provider = "anthropic"
    wrapper.model = type("M", (), {"model_id": "claude-haiku-4-5-20251001"})()
    wrapper._usage_session_maker = None
    wrapper.usage_limit_context = None
    wrapper._usage_limit_context = None
    wrapper._pii_redactor = None
    wrapper._pii_loaded = True

    async def _fake_create(**_kwargs):
        return _Stream([
            _Chunk("message_start",
                   message=type("M", (), {"usage": _Usage(five_m=0, one_h=WRITE)})()),
        ])

    wrapper.client.async_client.messages.create = _fake_create

    async for _ in llm_mod.LLM.inference_stream_v2(
        wrapper,
        messages=[Message(role="user", content="hi")],
        system="stable half",
        usage_scope="test.scope",
    ):
        pass

    assert captured, "the wrapper must record usage for the call"
    assert captured["cache_creation_tokens"] == WRITE
    assert captured["cache_write_1h_tokens"] == WRITE, (
        "the 1-hour attribution was dropped between the client and the recorder, "
        "which silently re-prices the write at 1.25x instead of 2x"
    )
    assert captured["cache_write_5m_tokens"] == 0

@pytest.mark.asyncio
async def test_a_later_delta_without_the_breakdown_does_not_clobber_the_split():
    """The bug a stubbed single-chunk stream cannot see.

    A real stream reports the breakdown once on message_start, then repeats
    cache_creation_input_tokens on message_delta WITHOUT it. Treating the
    absent breakdown as "all 5-minute" overwrote the correct attribution from
    the first chunk, so every streamed 1-hour write silently re-priced at 1.25x
    while the non-streaming path (one response, breakdown present) looked fine.
    """
    client = Anthropic(api_key="test-key-not-used")

    class _DeltaUsage:
        """message_delta: totals repeat, the breakdown does not."""
        input_tokens = 0
        output_tokens = 510
        cache_read_input_tokens = 0
        cache_creation_input_tokens = WRITE
        cache_creation = None

    async def _fake_create(**_kwargs):
        return _Stream([
            _Chunk("message_start",
                   message=type("M", (), {"usage": _Usage(five_m=0, one_h=WRITE)})()),
            _Chunk("message_delta",
                   delta=type("D", (), {"stop_reason": "end_turn"})(),
                   usage=_DeltaUsage()),
        ])

    client.async_client.messages.create = _fake_create
    events = [
        e async for e in client.inference_stream_v2(
            model_id="claude-haiku-4-5-20251001",
            messages=[Message(role="user", content="hi")],
            system="stable half",
        )
    ]
    usage = [e for e in events if isinstance(e, UsageEvent)][-1]

    assert usage.cache_creation_tokens == WRITE
    assert usage.cache_write_1h_tokens == WRITE, (
        "the delta's missing breakdown overwrote the 1-hour attribution read "
        "from message_start"
    )
    assert usage.cache_write_5m_tokens == 0

"""Codegen truncation detection.

When the coder's LLM stream stops at the model's output-token cap (e.g. a
write_csv call that inlines hundreds of literal rows), the collected text is
cut off mid-string. Executing it is a guaranteed "unterminated string literal"
SyntaxError whose feedback misleads the retry. The coder must instead raise
with an actionable message, which the executor's retry loop feeds back to the
next generation attempt.
"""
from __future__ import annotations

import pytest

from app.ai.agents.coder.coder import Coder, _TRUNCATION_ERROR, _is_truncation
from app.ai.llm.types import MessageStopEvent, TextDeltaEvent


class _FakeLLM:
    def __init__(self, events):
        self._events = events

    async def inference_stream_v2(self, **kwargs):
        for evt in self._events:
            yield evt


def _bare_coder(events, monkeypatch) -> Coder:
    coder = object.__new__(Coder)
    coder.llm = _FakeLLM(events)
    monkeypatch.setattr(Coder, "_time_context", lambda self: "now")
    return coder


def _transform_kwargs():
    return dict(
        prompt="build a table",
        schemas="",
        ds_clients={},
        excel_files=[],
        code_and_error_messages=[],
        memories="",
        previous_messages="",
        retries=0,
    )


@pytest.mark.asyncio
async def test_truncated_transform_stream_raises(monkeypatch):
    events = [
        TextDeltaEvent(text="def generate_df(ds_clients, excel_files):\n"),
        TextDeltaEvent(text='    rows = [("a", "unterminated'),
        MessageStopEvent(stop_reason="max_tokens"),
    ]
    coder = _bare_coder(events, monkeypatch)
    with pytest.raises(RuntimeError, match="output-token limit"):
        await coder.generate_transform_code(**_transform_kwargs())


@pytest.mark.asyncio
async def test_complete_transform_stream_returns_code(monkeypatch):
    code = "def generate_df(ds_clients, excel_files):\n    return None"
    events = [
        TextDeltaEvent(text=code),
        MessageStopEvent(stop_reason="end_turn"),
    ]
    coder = _bare_coder(events, monkeypatch)
    result = await coder.generate_transform_code(**_transform_kwargs())
    assert "def generate_df" in result


def test_is_truncation_only_matches_max_tokens():
    assert _is_truncation(MessageStopEvent(stop_reason="max_tokens"))
    assert not _is_truncation(MessageStopEvent(stop_reason="end_turn"))
    assert not _is_truncation(TextDeltaEvent(text="max_tokens"))
    assert _TRUNCATION_ERROR  # message exists and is non-empty

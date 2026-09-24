"""Reasoning time inside create_data's code generation.

The chat used to label a create_data block "Thought for <tool duration>" —
code generation plus execution, presented as reasoning. Code generation now
measures the time the model actually spent in reasoning blocks, so the label
and the trace can tell thinking apart from writing and running code.

The provider stream is the mocked boundary; the clock is injected so the
assertions do not depend on wall time.
"""
import pytest

import app.ai.agents.coder.coder as coder_module
from app.ai.agents.coder.coder import Coder
from app.ai.llm.types import (
    MessageStopEvent,
    ReasoningCompleteEvent,
    ReasoningDeltaEvent,
    ReasoningStartEvent,
    TextDeltaEvent,
)
from app.ai.schemas.codegen import CodeGenContext


class _Clock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now


class _ScriptedLLM:
    """Yields (seconds_elapsed_before, event) pairs, advancing the clock."""

    def __init__(self, clock, script):
        self.clock = clock
        self.script = script

    async def inference_stream_v2(self, **kwargs):
        for advance, event in self.script:
            self.clock.now += advance
            yield event


class _Settings:
    def get_config(self, key, default=None):
        return default


CODE = "def generate_df(ds_clients, excel_files):\n    import pandas as pd\n    return pd.DataFrame()\n"


def _coder(clock, script) -> Coder:
    c = Coder.__new__(Coder)
    c.llm = _ScriptedLLM(clock, script)
    c.organization_settings = _Settings()
    c.enable_llm_see_data = False
    c.instruction_context_builder = None
    c.context_hub = None
    return c


async def _generate(coder):
    await coder.generate_code(
        data_model=None, prompt="sales by month", interpreted_prompt="sales by month",
        schemas="<schemas/>", ds_clients={}, excel_files=[], code_and_error_messages=[],
        memories="", previous_messages=[], retries=0,
        context=CodeGenContext(user_prompt="sales by month", schemas_excerpt="<schemas/>"),
    )
    return coder.reasoning_ms


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(coder_module, "time", c)
    return c


@pytest.mark.asyncio
@pytest.mark.parametrize("think_s,write_s", [(3.5, 5.0), (12.0, 0.5)])
async def test_counts_reasoning_but_not_code_writing(clock, think_s, write_s):
    script = [
        (0.4, ReasoningStartEvent()),
        (think_s / 2, ReasoningDeltaEvent(text="plan the query")),
        (think_s / 2, ReasoningCompleteEvent(text="plan the query")),
        (write_s, TextDeltaEvent(text=CODE)),
        (0.1, MessageStopEvent(stop_reason="end_turn")),
    ]
    assert await _generate(_coder(clock, script)) == pytest.approx(think_s * 1000, abs=1)


@pytest.mark.asyncio
async def test_several_reasoning_blocks_add_up(clock):
    script = [
        (0.0, ReasoningStartEvent()), (2.0, ReasoningCompleteEvent(text="a")),
        (1.0, TextDeltaEvent(text="x")),
        (0.0, ReasoningStartEvent()), (3.0, ReasoningCompleteEvent(text="b")),
        (1.0, TextDeltaEvent(text=CODE)),
    ]
    assert await _generate(_coder(clock, script)) == pytest.approx(5000, abs=1)


@pytest.mark.asyncio
async def test_block_without_complete_event_ends_at_first_output(clock):
    script = [
        (0.0, ReasoningDeltaEvent(text="thinking")),
        (4.0, TextDeltaEvent(text=CODE)),
        (9.0, TextDeltaEvent(text="\n")),
    ]
    assert await _generate(_coder(clock, script)) == pytest.approx(4000, abs=1)


@pytest.mark.asyncio
async def test_each_generation_is_timed_on_its_own(clock):
    """A retry must not inherit the previous attempt's reasoning."""
    coder = _coder(clock, [
        (0.0, ReasoningStartEvent()), (6.0, ReasoningCompleteEvent(text="a")),
        (1.0, TextDeltaEvent(text=CODE)),
    ])
    assert await _generate(coder) == pytest.approx(6000, abs=1)
    coder.llm.script = [(2.0, TextDeltaEvent(text=CODE))]
    assert await _generate(coder) == 0

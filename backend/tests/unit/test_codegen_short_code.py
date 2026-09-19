"""The generated-code length loop.

Generated code was long for three reasons that all live in the codegen prompt
and executor, not in the model: the prompt mandated a print after every
query and column change, it had no brevity rule, and it pasted past code
verbatim as the style reference. Meanwhile the prints never reached a retry —
the executor read stdout only after a successful run. These tests pin the
new contract: one print, a brevity rule, short examples only, and the tail of
stdout attached to a failed attempt's feedback.
"""
import asyncio

import pytest

from app.ai.agents.coder.coder import _MAX_EXAMPLE_SNIPPET_LINES, Coder
from app.ai.code_execution.code_execution import (
    StreamingCodeExecutor,
    format_execution_failure,
)
from app.ai.schemas.codegen import CodeGenContext, CodeGenRequest

# ---------------------------------------------------------------------------
# Executor: stdout survives a failed run and is fed to the retry
# ---------------------------------------------------------------------------

PRINT_THEN_RAISE = (
    "def generate_df(ds_clients, excel_files):\n"
    "    import pandas as pd\n"
    "    df = pd.DataFrame({'a': [1, 2]})\n"
    "    print('Final df:', df.shape, list(df.columns))\n"
    "    raise ValueError('boom after print')\n"
)

CLEAN = (
    "def generate_df(ds_clients, excel_files):\n"
    "    import pandas as pd\n"
    "    return pd.DataFrame({'a': [1]})\n"
)


def _drive(code_sequence, retries=2):
    calls = []

    async def _gen(**kwargs):
        calls.append(kwargs)
        return code_sequence[min(len(calls) - 1, len(code_sequence) - 1)]

    events = []

    async def go():
        async for ev in StreamingCodeExecutor().generate_and_execute_stream_v2(
            request=CodeGenRequest(
                context=CodeGenContext(user_prompt="x", schemas_excerpt=""),
                retries=retries,
            ),
            ds_clients={},
            excel_files=[],
            code_generator_fn=_gen,
        ):
            events.append(ev)

    asyncio.run(go())
    return calls, events


class TestStdoutReachesRetry:
    def test_retry_feedback_carries_stdout_tail(self):
        calls, events = _drive([PRINT_THEN_RAISE, CLEAN])
        assert len(calls) == 2
        _code, error = calls[1]["code_and_error_messages"][-1]
        assert "boom after print" in error
        assert "<stdout_before_failure>" in error
        assert "Final df: (2, 1) ['a']" in error

    def test_ui_message_stays_the_bare_error(self):
        """The chat shows the error; the evidence goes to the coder only."""
        _calls, events = _drive([PRINT_THEN_RAISE, CLEAN])
        stdout_events = [e["payload"] for e in events if e["type"] == "stdout"]
        assert any("boom after print" in m for m in stdout_events)
        assert not any("<stdout_before_failure>" in m for m in stdout_events)

    def test_no_output_means_no_section(self):
        exc = RuntimeError("x")
        assert format_execution_failure(exc, "Execution error: x") == "Execution error: x"

    def test_tail_is_bounded(self):
        from app.ai.code_execution.code_execution import FAILURE_STDOUT_TAIL_CHARS
        loud = (
            "def generate_df(ds_clients, excel_files):\n"
            "    for i in range(5000):\n"
            "        print('row', i)\n"
            "    raise ValueError('late')\n"
        )
        calls, _ = _drive([loud, CLEAN])
        _code, error = calls[1]["code_and_error_messages"][-1]
        tail = error.split("<stdout_before_failure>", 1)[1]
        assert len(tail) <= FAILURE_STDOUT_TAIL_CHARS + len("\n</stdout_before_failure>") + 2
        assert "row 4999" in tail  # the END of the output is what survives


# ---------------------------------------------------------------------------
# Prompt: brevity rule, single print, bounded master-table bias, short examples
# ---------------------------------------------------------------------------

class _StubLLM:
    def __init__(self):
        self.prompts = []

    async def inference_stream_v2(self, messages, system=None, **kwargs):
        # generate_code splits its prompt into a cacheable `system` half and a
        # per-call user half; record both so assertions stay channel-agnostic.
        self.prompts.append(f"{system or ''}\n{messages[0].content}")
        return
        yield  # pragma: no cover


class _StubSettings:
    def get_config(self, key, default=None):
        return default


class _SnippetBuilder:
    def __init__(self, snippets):
        self._snippets = snippets

    async def get_top_successful_snippets_for_tables(self, tables_by_source, top_k=2):
        return self._snippets


def _coder(see_data=False, builder=None) -> Coder:
    c = Coder.__new__(Coder)
    c.llm = _StubLLM()
    c.organization_settings = _StubSettings()
    c.enable_llm_see_data = see_data
    c.instruction_context_builder = None
    c.context_hub = None
    return c


async def _prompt(coder, builder=None, **ctx):
    base = {"user_prompt": "total sales by country", "schemas_excerpt": "<schemas/>"}
    base.update(ctx)
    await coder.generate_code(
        data_model=None,
        prompt=base["user_prompt"],
        interpreted_prompt=base["user_prompt"],
        schemas="<schemas/>",
        ds_clients={},
        excel_files=[],
        code_and_error_messages=[],
        memories="",
        previous_messages=[],
        retries=0,
        code_context_builder=builder,
        context=CodeGenContext(**base),
    )
    return coder.llm.prompts[0]


@pytest.mark.asyncio
async def test_prompt_has_brevity_rule_and_single_print():
    text = await _prompt(_coder())
    assert "Keep the code short" in text
    assert "no try/except" in text
    assert "the ONLY print in the function" in text
    # The per-query / per-column-change prints are gone.
    assert "After each query or DataFrame creation" not in text
    assert "After any operation that changes DataFrame columns" not in text
    assert text.count('print("Final df:"') == 1


@pytest.mark.asyncio
async def test_preview_rides_the_single_print_when_llm_may_see_data():
    text = await _prompt(_coder(see_data=True))
    assert 'print("Final df:", df.shape, list(df.columns)); print(df.head())' in text
    assert "after each query" not in text.lower()


@pytest.mark.asyncio
async def test_master_table_bias_is_bounded():
    text = await _prompt(_coder())
    assert "Slight master-table bias, bounded" in text
    assert "at most 2-3 slicing columns" in text
    assert "Never add a join, a subquery or a second query just to bring in an extra column" in text


@pytest.mark.asyncio
async def test_long_past_snippets_are_not_used_as_examples():
    short = {"step_id": "s1", "score": 1, "success_rate": 1.0,
             "code": "def generate_df(ds_clients, excel_files):\n    return df\n"}
    long_code = "def generate_df(ds_clients, excel_files):\n" + "    x = 1\n" * (_MAX_EXAMPLE_SNIPPET_LINES + 5)
    long = {"step_id": "s2", "score": 2, "success_rate": 1.0, "code": long_code}
    text = await _prompt(
        _coder(), builder=_SnippetBuilder([long, short]),
        tables_by_source=[{"data_source": "ds", "tables": ["orders"]}],
    )
    assert "step_id=s1" in text
    assert "step_id=s2" not in text


@pytest.mark.asyncio
async def test_only_long_snippets_means_no_examples_section_content():
    long_code = "def generate_df(ds_clients, excel_files):\n" + "    x = 1\n" * (_MAX_EXAMPLE_SNIPPET_LINES + 5)
    text = await _prompt(
        _coder(), builder=_SnippetBuilder([{"step_id": "s2", "code": long_code}]),
        tables_by_source=[{"data_source": "ds", "tables": ["orders"]}],
    )
    assert "SUCCESSFUL EXAMPLES" not in text

"""Sandbox feedback loop for code-security violations.

Contract under test: when generated Python trips the AST validator
(`unsafe_python`, e.g. a `getattr()` call), the executor must treat it as a
correctable codegen failure — retry within the attempt budget — and the next
code-generation prompt must actually contain the previous code + error so the
coder can self-correct instead of re-rolling blind.

Born as the reproduction for: xls inspection failing repeatedly with
"Security violation (unsafe_python): Forbidden function call: 'getattr()'".
"""

import asyncio

import pandas as pd

from app.ai.agents.coder.coder import Coder
from app.ai.code_execution.code_execution import (
    FORBIDDEN_BUILTINS,
    StreamingCodeExecutor,
)
from app.ai.llm.types import TextDeltaEvent
from app.ai.schemas.codegen import CodeGenContext, CodeGenRequest


GETATTR_CODE = (
    "def generate_df(ds_clients, excel_files):\n"
    "    import pandas as pd\n"
    "    path = getattr(excel_files, 'path', None)\n"
    "    return pd.DataFrame({'a': [1]})\n"
)

CLEAN_CODE = (
    "def generate_df(ds_clients, excel_files):\n"
    "    import pandas as pd\n"
    "    return pd.DataFrame({'a': [1]})\n"
)

VIOLATION_MSG = (
    "Security violation (unsafe_python): Code contains forbidden constructs: "
    "Forbidden function call: 'getattr()'"
)


def _capturing_coder(code_sequence):
    """code_generator_fn stub that records the kwargs of every call."""
    calls = []

    async def _gen(**kwargs):
        calls.append(kwargs)
        i = min(len(calls) - 1, len(code_sequence) - 1)
        return code_sequence[i]

    return _gen, calls


def _drive(executor, generator_fn, retries=2):
    events = []

    async def go():
        async for ev in executor.generate_and_execute_stream_v2(
            request=CodeGenRequest(
                context=CodeGenContext(user_prompt="x", schemas_excerpt=""),
                retries=retries,
            ),
            ds_clients={},
            excel_files=[],
            code_generator_fn=generator_fn,
        ):
            events.append(ev)

    asyncio.run(go())
    return events


class TestUnsafePythonIsRetryable:
    def test_violation_then_clean_code_succeeds(self):
        """An unsafe_python violation must consume a retry, not end the run."""
        gen, calls = _capturing_coder([GETATTR_CODE, CLEAN_CODE])
        events = _drive(StreamingCodeExecutor(), gen, retries=2)

        done = [e for e in events if e["type"] == "done"][-1]["payload"]
        assert len(calls) == 2, "executor must regenerate after an unsafe_python violation"
        df = done["df"]
        assert df is not None and list(df.columns) == ["a"]
        # The violation is still recorded for the caller/audit trail.
        assert any("unsafe_python" in (err or "") for _, err in done["errors"])

    def test_violation_still_emits_security_event(self):
        """Retrying must not silence the audit event."""
        gen, _ = _capturing_coder([GETATTR_CODE, CLEAN_CODE])
        events = _drive(StreamingCodeExecutor(), gen, retries=2)

        sec = [e for e in events if e["type"] == "security_violation"]
        assert len(sec) == 1
        assert sec[0]["payload"]["violation_type"] == "unsafe_python"

    def test_retry_receives_violation_feedback(self):
        """The 2nd codegen call must see the offending code and the violation."""
        gen, calls = _capturing_coder([GETATTR_CODE, CLEAN_CODE])
        _drive(StreamingCodeExecutor(), gen, retries=2)

        assert len(calls) == 2
        feedback = calls[1]["code_and_error_messages"]
        assert feedback, "previous attempt must be fed back to the coder"
        prev_code, prev_error = feedback[-1]
        assert "getattr" in prev_code
        assert "unsafe_python" in prev_error

    def test_budget_still_bounds_violations(self):
        """A coder that always emits violations must stop at the retry budget."""
        gen, calls = _capturing_coder([GETATTR_CODE])
        events = _drive(StreamingCodeExecutor(), gen, retries=2)

        done = [e for e in events if e["type"] == "done"][-1]["payload"]
        assert len(calls) == 2, "must not loop beyond the configured budget"
        assert done["df"] is None
        assert len(done["errors"]) == 2


class _PromptCapturingLLM:
    """Stands in for Coder.llm — records the rendered prompt."""

    def __init__(self):
        self.prompts = []

    async def inference_stream_v2(self, messages, **kwargs):
        self.prompts.append(messages[0].content)
        yield TextDeltaEvent(text=CLEAN_CODE)


def _bare_coder():
    coder = Coder.__new__(Coder)  # skip __init__: it needs a live LLM provider
    coder.llm = _PromptCapturingLLM()
    coder.organization_settings = None
    coder.enable_llm_see_data = True
    coder.instruction_context_builder = None
    coder.context_hub = None
    return coder


def _ctx(**overrides):
    base = dict(user_prompt="inspect the excel file", schemas_excerpt="")
    base.update(overrides)
    return CodeGenContext(**base)


class TestErrorFeedbackReachesPrompts:
    """The prompt templates must render code_and_error_messages, not drop them."""

    def _feedback(self):
        return [(GETATTR_CODE, VIOLATION_MSG)]

    def test_inspection_prompt_contains_previous_error(self):
        coder = _bare_coder()
        asyncio.run(
            coder.generate_inspection_code(
                prompt="p", schemas="", ds_clients={}, excel_files=[],
                code_and_error_messages=self._feedback(),
                memories="", previous_messages="", retries=1,
                context=_ctx(),
            )
        )
        prompt = coder.llm.prompts[0]
        assert VIOLATION_MSG in prompt
        assert "getattr(excel_files, 'path', None)" in prompt

    def test_create_data_prompt_contains_previous_error(self):
        coder = _bare_coder()
        asyncio.run(
            coder.generate_code(
                data_model={}, prompt="p", interpreted_prompt="p", schemas="",
                ds_clients={}, excel_files=[],
                code_and_error_messages=self._feedback(),
                memories="", previous_messages="", retries=1,
                context=_ctx(),
            )
        )
        prompt = coder.llm.prompts[0]
        assert VIOLATION_MSG in prompt
        assert "getattr(excel_files, 'path', None)" in prompt


class TestSandboxRulesInPrompts:
    """Both codegen prompts must state the sandbox's forbidden builtins,
    kept in sync with the validator's own list."""

    def test_inspection_prompt_lists_forbidden_builtins(self):
        coder = _bare_coder()
        asyncio.run(
            coder.generate_inspection_code(
                prompt="p", schemas="", ds_clients={}, excel_files=[],
                code_and_error_messages=[], memories="", previous_messages="",
                retries=0, context=_ctx(),
            )
        )
        prompt = coder.llm.prompts[0]
        missing = sorted(b for b in FORBIDDEN_BUILTINS if b not in prompt)
        assert not missing, f"inspection prompt missing sandbox rules for: {missing}"

    def test_create_data_prompt_lists_forbidden_builtins(self):
        coder = _bare_coder()
        asyncio.run(
            coder.generate_code(
                data_model={}, prompt="p", interpreted_prompt="p", schemas="",
                ds_clients={}, excel_files=[],
                code_and_error_messages=[], memories="", previous_messages="",
                retries=0, context=_ctx(),
            )
        )
        prompt = coder.llm.prompts[0]
        missing = sorted(b for b in FORBIDDEN_BUILTINS if b not in prompt)
        assert not missing, f"create_data prompt missing sandbox rules for: {missing}"


class TestInspectionPromptSeesLastFailedObservation:
    """When the planner re-invokes inspect_data after a failed attempt, the
    inspection prompt must surface the previous failure from last_observation."""

    def test_failed_last_observation_rendered(self):
        coder = _bare_coder()
        last_obs = {
            "tool_name": "inspect_data",
            "tool_input": {"user_prompt": "count DMCH rows"},
            "observation": {
                "success": False,
                "summary": "Inspection failed for: count DMCH rows",
                "error": {"type": "execution_failure", "message": VIOLATION_MSG},
                "code": GETATTR_CODE,
            },
        }
        asyncio.run(
            coder.generate_inspection_code(
                prompt="p", schemas="", ds_clients={}, excel_files=[],
                code_and_error_messages=[], memories="", previous_messages="",
                retries=0, context=_ctx(last_observation=last_obs),
            )
        )
        prompt = coder.llm.prompts[0]
        assert VIOLATION_MSG in prompt

    def test_successful_last_observation_not_dumped(self):
        """A prior success is noise for a quick inspection — don't inject it."""
        coder = _bare_coder()
        last_obs = {
            "tool_name": "create_data",
            "observation": {"success": True, "summary": "built Customer Sales"},
        }
        asyncio.run(
            coder.generate_inspection_code(
                prompt="p", schemas="", ds_clients={}, excel_files=[],
                code_and_error_messages=[], memories="", previous_messages="",
                retries=0, context=_ctx(last_observation=last_obs),
            )
        )
        prompt = coder.llm.prompts[0]
        assert "built Customer Sales" not in prompt

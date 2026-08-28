"""Artifact feedback-loop fixes.

Covers the production-incident fixes from the 2026-08-05 harness-export
investigation:

1. ToolRunner no longer validates failure envelopes (success=False) against
   the SUCCESS output model — doing so replaced meaningful failure
   observations (DATA_GAP remediation, unmatched-diff details) with a generic
   "Output validation failed".
2. ToolRunner never retries non-recoverable errors (context window exceeded,
   provider credit exhaustion) — production replayed identical oversized
   prompts and a credit failure twice.
3. Page-mode edit prompts no longer carry the accumulated spec or the
   conversation history (they grew edit input from 38K to 63K tokens by
   v11+), and the static reference moved to a cacheable system prompt.
4. remove_visualization_ids exists on the edit input, and removed
   visualizations produce explicit delete + re-index instructions.
5. fatal_render_errors gates only thrown errors; console errors are advisory.
6. Visualization profiles carry the true dataset size and the sample size as
   separate fields.
"""
from __future__ import annotations

from typing import AsyncIterator, Type

import pytest
from pydantic import BaseModel, Field

from app.ai.runner.tool_runner import ToolRunner, is_non_recoverable_error
from app.ai.runner.policies import RetryPolicy


# ─── 1. Failure envelopes bypass success-model validation ────────────────────

class _StrictOutput(BaseModel):
    artifact_id: str = Field(...)
    code: str = Field(...)
    version: int = Field(...)


class _FailingEnvelopeTool:
    """Returns a failure envelope that would NOT validate as _StrictOutput."""

    name = "failing_envelope_tool"
    spec = None
    metadata = None
    input_model = None
    output_model = _StrictOutput

    async def run_stream(self, tool_input, runtime_ctx) -> AsyncIterator[dict]:
        yield {
            "type": "tool.end",
            "payload": {
                "output": {"success": False, "artifact_id": "a1", "error": "data gap"},
                "observation": {
                    "summary": "Edit blocked — data gap: missing customer_id",
                    "error": {
                        "type": "data_gap",
                        "message": "missing customer_id",
                        "remediation": "Recreate via create_data, do NOT fall back to create_artifact.",
                    },
                },
            },
        }


class _InvalidSuccessTool:
    """Returns a successful-looking output that fails the output model."""

    name = "invalid_success_tool"
    spec = None
    metadata = None
    input_model = None
    output_model = _StrictOutput

    async def run_stream(self, tool_input, runtime_ctx) -> AsyncIterator[dict]:
        yield {
            "type": "tool.end",
            "payload": {
                "output": {"artifact_id": "a1"},  # missing code/version
                "observation": {"summary": "looks done"},
            },
        }


async def _noop_emit(event):
    return None


@pytest.mark.asyncio
async def test_failure_envelope_preserves_real_observation():
    runner = ToolRunner()
    result = await runner.run(_FailingEnvelopeTool(), {}, {"mode": "chat"}, _noop_emit)

    observation = result["observation"]
    assert observation["error"]["type"] == "data_gap"
    assert "create_data" in observation["error"]["remediation"]
    assert result["output"]["success"] is False


@pytest.mark.asyncio
async def test_invalid_success_output_still_fails_validation():
    runner = ToolRunner()
    result = await runner.run(_InvalidSuccessTool(), {}, {"mode": "chat"}, _noop_emit)

    assert result["error"]["type"] == "output_validation_error"


# ─── 2. Non-recoverable errors are never retried ─────────────────────────────

class _ContextLengthTool:
    name = "context_length_tool"
    spec = None
    metadata = None
    input_model = None
    output_model = None
    attempts = 0

    async def run_stream(self, tool_input, runtime_ctx) -> AsyncIterator[dict]:
        type(self).attempts += 1
        raise RuntimeError("This model's maximum context length is 200000 tokens; your prompt is too long")
        yield  # pragma: no cover


class _TransientErrorTool:
    name = "transient_error_tool"
    spec = None
    metadata = None
    input_model = None
    output_model = None
    attempts = 0

    async def run_stream(self, tool_input, runtime_ctx) -> AsyncIterator[dict]:
        type(self).attempts += 1
        raise RuntimeError("connection reset by peer")
        yield  # pragma: no cover


def test_non_recoverable_error_patterns():
    assert is_non_recoverable_error("maximum context length exceeded")
    assert is_non_recoverable_error("Your credit balance is too low")
    assert is_non_recoverable_error("You exceeded your current quota")
    assert not is_non_recoverable_error("connection reset by peer")
    assert not is_non_recoverable_error(None)


@pytest.mark.asyncio
async def test_context_length_error_not_retried():
    _ContextLengthTool.attempts = 0
    runner = ToolRunner(retry=RetryPolicy(max_attempts=2, backoff_ms=1, jitter_ms=0))
    result = await runner.run(_ContextLengthTool(), {}, {"mode": "chat"}, _noop_emit)

    assert _ContextLengthTool.attempts == 1
    assert result["error"]["non_recoverable"] is True
    assert result["retry_exhausted"] is True
    assert "analysis_complete" not in result
    assert "final_answer" not in result


@pytest.mark.asyncio
async def test_transient_error_still_retried():
    _TransientErrorTool.attempts = 0
    runner = ToolRunner(retry=RetryPolicy(max_attempts=2, backoff_ms=1, jitter_ms=0))
    result = await runner.run(_TransientErrorTool(), {}, {"mode": "chat"}, _noop_emit)

    assert _TransientErrorTool.attempts == 2
    assert "non_recoverable" not in result["error"]
    assert result["retry_exhausted"] is True
    assert "analysis_complete" not in result


# ─── 3. Edit prompt slimming + system split ──────────────────────────────────

def _build_page_edit_prompt(**overrides):
    from app.ai.tools.implementations.edit_artifact_legacy import EditArtifactTool

    tool = EditArtifactTool()
    kwargs = dict(
        existing_code="<script type=\"text/babel\">function App(){return null}</script>",
        edit_prompt="Make the KPI row blue",
        mode="page",
        viz_profiles=[{"id": "v1", "title": "Revenue", "row_count": 5000, "sample_row_count": 100}],
        instructions_context="",
        messages_context="user: hello\nassistant: hi",
        report_title="Q3",
        original_spec="Build a dashboard\n+ Edit (v2): add filters",
    )
    kwargs.update(overrides)
    return tool, tool._build_edit_prompt(**kwargs)


def test_page_edit_prompt_omits_history_and_accumulated_spec():
    _, prompt = _build_page_edit_prompt()
    assert "Conversation History" not in prompt
    assert "ORIGINAL DASHBOARD SPEC" not in prompt
    assert "Make the KPI row blue" in prompt


def test_page_edit_prompt_static_reference_lives_in_system_prompt():
    tool, prompt = _build_page_edit_prompt()
    system = tool._build_edit_system_prompt()
    # Diff output contract + sandbox reference are static → system prompt
    assert "<<<<<<< SEARCH" in system
    assert "useArtifactData" in system
    assert "<<<<<<< SEARCH" not in prompt


def test_page_edit_prompt_includes_render_error_feedforward():
    _, prompt = _build_page_edit_prompt(
        prev_render_errors=["ReferenceError: fmtX is not defined"],
    )
    assert "KNOWN RENDER ERRORS" in prompt
    assert "fmtX is not defined" in prompt


# ─── 4. Visualization removal contract ───────────────────────────────────────

def test_edit_input_accepts_remove_visualization_ids():
    from app.ai.tools.schemas.edit_artifact import EditArtifactInput

    data = EditArtifactInput(
        artifact_id="a1",
        edits=[{"find": "<PieChart />", "replace": ""}],
        remove_visualization_ids=["v2"],
    )
    assert data.remove_visualization_ids == ["v2"]


def test_edit_input_rejects_legacy_edit_prompt_with_guidance():
    # The pre-mechanical edit_artifact took an English edit_prompt; a model
    # imitating old history must get actionable guidance, not a bare
    # missing-field error.
    import pytest as _pytest
    from app.ai.tools.schemas.edit_artifact import EditArtifactInput

    with _pytest.raises(Exception) as exc:
        EditArtifactInput(artifact_id="a1", edit_prompt="remove the pie chart")
    assert "find/replace" in str(exc.value)


def test_removed_vizs_produce_delete_and_reindex_instructions():
    _, prompt = _build_page_edit_prompt(
        removed_vizs=[{"id": "v2", "title": "Sales by Region"}],
    )
    assert "REMOVED VISUALIZATIONS" in prompt
    assert "Sales by Region" in prompt
    assert "re-indexed" in prompt


# ─── 5. Fatal vs advisory render errors ──────────────────────────────────────

def test_fatal_render_errors_ignores_console_entries():
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool

    errors = [
        "[console.error] Warning: Each child in a list should have a unique key",
        "ReferenceError: x is not defined",
    ]
    fatal = CreateArtifactTool.fatal_render_errors(errors)
    assert fatal == ["ReferenceError: x is not defined"]
    assert CreateArtifactTool.fatal_render_errors([]) == []


# ─── 6. Sample vs total row counts ───────────────────────────────────────────

def test_viz_profile_reports_true_and_sample_row_counts():
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool

    tool = CreateArtifactTool()
    viz = {
        "id": "v1",
        "title": "Revenue",
        "row_count": 5000,          # true dataset size (from full step rows)
        "sample_row_count": 100,    # capped sample
        "rows": [{"a": 1}] * 100,
        "columns": [{"field": "a"}],
    }
    profile = tool._build_viz_profile(viz, allow_llm_see_data=True)
    assert profile["row_count"] == 5000
    assert profile["sample_row_count"] == 100


def test_create_page_system_prompt_documents_sample_contract():
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool

    system = CreateArtifactTool()._build_page_system_prompt()
    assert "Sample vs full data" in system
    assert "row_count" in system

"""Regression: the planner v3 prompt must keep report-level context in cached
system blocks, not in the per-turn user message.

Feedback loop: docs/feedback-loops/report-context-ttft-growth.md

Submit -> first-token latency grew with report length because the whole report
context (schemas, instructions, files, resources) was sent as an UNCACHED user
message on every planner call. The v3 builder now splits it into system blocks
(most stable first) and the Anthropic client marks each with a prompt-cache
breakpoint, so only genuinely per-turn content is re-processed per call.
"""
import pytest

from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.schemas.ai.planner import PlannerInput


def _planner_input(**overrides) -> PlannerInput:
    base = dict(
        organization_name="Org",
        organization_ai_analyst_name="Analyst",
        instructions="<instructions>always be nice</instructions>",
        user_message="show revenue by month",
        schemas_combined="<data_sources><table name=\"invoices\"/></data_sources>",
        files_context="<files>report.xlsx</files>",
        resources_combined="<resources>dbt models</resources>",
        history_summary="",
        messages_context="User (10:00): hi",
        tool_catalog=[],
        mode="chat",
    )
    base.update(overrides)
    return PlannerInput(**base)


def test_report_context_moves_to_system_blocks():
    v3 = PromptBuilderV3.build(_planner_input())

    assert v3.system_blocks and len(v3.system_blocks) >= 2
    user_msg = v3.messages[0]["content"]

    # The stable report context must NOT ride the uncached user message.
    for stable_chunk in (
        "<data_sources>",  # schemas
        "<files>report.xlsx</files>",
        "<resources>dbt models</resources>",
        "always be nice",  # instructions
    ):
        assert stable_chunk not in user_msg, f"{stable_chunk!r} leaked into the user message"
        assert any(stable_chunk in b for b in v3.system_blocks), (
            f"{stable_chunk!r} missing from system blocks"
        )

    # Per-turn content stays in the user message.
    assert "show revenue by month" in user_msg
    assert "User (10:00): hi" in user_msg

    # Joined system stays byte-equivalent to the blocks (non-caching providers).
    assert v3.system == "\n\n".join(v3.system_blocks)


def test_block_order_is_most_stable_first():
    """Cache prefixes only survive while earlier blocks stay byte-identical:
    behavior prompt (static) -> report context (stable per report) ->
    instructions (stable per turn)."""
    v3 = PromptBuilderV3.build(_planner_input())
    blocks = v3.system_blocks
    idx_system = next(i for i, b in enumerate(blocks) if "You are an AI data analyst" in b)
    idx_report = next(i for i, b in enumerate(blocks) if "<data_sources>" in b)
    idx_instr = next(i for i, b in enumerate(blocks) if "always be nice" in b)
    assert idx_system < idx_report < idx_instr


def test_empty_context_produces_single_block():
    v3 = PromptBuilderV3.build(_planner_input(
        instructions="", schemas_combined="", files_context="", resources_combined="",
    ))
    assert v3.system_blocks and len(v3.system_blocks) == 1
    assert v3.system == v3.system_blocks[0]


def test_anthropic_client_marks_each_system_block():
    """The Anthropic request must carry a cache_control breakpoint per system
    block (max 3 — the 4th allowed breakpoint is spent on the last tool)."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.ai.llm.clients.anthropic_client import Anthropic
    from app.ai.llm.types import Message

    client = Anthropic(api_key="test-key")

    captured: dict = {}

    class _FakeStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    async def _fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeStream()

    async def _run():
        sdk = MagicMock()
        sdk.messages.create = AsyncMock(side_effect=_fake_create)
        with patch.object(client, "async_client", sdk):
            events = client.inference_stream_v2(
                model_id="claude-haiku-4-5-20251001",
                messages=[Message(role="user", content="hi")],
                system=["STATIC PROMPT", "REPORT CONTEXT", "TURN INSTRUCTIONS"],
            )
            async for _ in events:
                pass

    asyncio.run(_run())

    system = captured.get("system")
    assert isinstance(system, list) and len(system) == 3
    for block in system:
        assert block.get("cache_control") == {"type": "ephemeral"}
    texts = [b["text"] for b in system]
    assert texts == ["STATIC PROMPT", "REPORT CONTEXT", "TURN INSTRUCTIONS"]

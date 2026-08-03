"""The opt-in transcript path in PromptBuilderV3.

Contract: same context and tools reach the model either way; only the message
shape changes. Off by default.
"""
import json
import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest  # noqa: E402

from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3  # noqa: E402
from app.schemas.ai.planner import PlannerInput  # noqa: E402


def _input(obs_count=3, **kw):
    obs = []
    for i in range(obs_count):
        obs.append({
            "execution_number": i,
            "tool_name": "read_file",
            "loop_index": i,
            "tool_input": {"file_id": f"f{i}.csv"},
            "observation": {
                "summary": f"read file {i}",
                "success": True,
                "file_id": f"f{i}.csv",
                "details": "D" * 500,
            },
        })
    kw.setdefault("schemas_combined", "<schemas><table name='sales'/></schemas>")
    kw.setdefault("instructions", "<instructions>rule one</instructions>")
    return PlannerInput(
        organization_name="Org",
        organization_ai_analyst_name="AI",
        timezone="UTC",
        user_message="what is total revenue?",
        past_observations=obs,
        last_observation=obs[-1]["observation"] if obs else None,
        tool_catalog=[],
        mode="chat",
        **kw,
    )


def _blob(v3):
    return json.dumps(v3.messages, default=str)


def test_default_path_is_unchanged():
    v3 = PromptBuilderV3.build(_input())
    assert len(v3.messages) == 1
    assert isinstance(v3.messages[0]["content"], str)


def test_transcript_path_produces_multiple_turns():
    v3 = PromptBuilderV3.build(_input(use_transcript=True))
    assert len(v3.messages) > 1
    roles = [m["role"] for m in v3.messages]
    assert "assistant" in roles, "prior steps must replay as assistant turns"


def test_transcript_path_emits_native_tool_blocks():
    v3 = PromptBuilderV3.build(_input(use_transcript=True))
    blocks = [b for m in v3.messages if isinstance(m["content"], list) for b in m["content"]]
    kinds = {b.get("type") for b in blocks}
    assert "tool_use" in kinds
    assert "tool_result" in kinds


def test_no_past_observations_block_on_transcript_path():
    v3 = PromptBuilderV3.build(_input(use_transcript=True))
    assert "<past_observations>" not in _blob(v3)
    assert "<last_observation>" not in _blob(v3)


def test_context_survives_the_switch():
    """Whatever the shape, the model must still get instructions + schemas."""
    plain = _blob(PromptBuilderV3.build(_input()))
    trans = _blob(PromptBuilderV3.build(_input(use_transcript=True)))
    for needle in ("rule one", "sales", "what is total revenue?"):
        assert needle in plain, f"{needle} missing from default path"
        assert needle in trans, f"{needle} missing from transcript path"


def test_tool_calls_and_args_are_replayed():
    blob = _blob(PromptBuilderV3.build(_input(use_transcript=True)))
    assert "read_file" in blob
    assert "f0.csv" in blob


def test_clock_rides_at_the_tail():
    v3 = PromptBuilderV3.build(_input(use_transcript=True))
    blob = _blob(v3)
    assert "<time>" in blob
    first = json.dumps(v3.messages[0], default=str)
    assert "<time>" not in first, "the clock must not sit in the cacheable head"


def test_parallel_batch_becomes_one_turn_pair():
    obs = [
        {"execution_number": i, "tool_name": t, "loop_index": 0,
         "tool_input": {}, "observation": {"summary": f"ran {t}", "success": True}}
        for i, t in enumerate(("read_file", "list_files"))
    ]
    pi = PlannerInput(
        organization_name="Org", organization_ai_analyst_name="AI", timezone="UTC",
        user_message="go", past_observations=obs, tool_catalog=[], mode="chat",
        use_transcript=True,
    )
    v3 = PromptBuilderV3.build(pi)
    calls = [b for m in v3.messages if isinstance(m["content"], list)
             for b in m["content"] if b.get("type") == "tool_use"]
    results = [b for m in v3.messages if isinstance(m["content"], list)
               for b in m["content"] if b.get("type") == "tool_result"]
    assert len(calls) == 2 and len(results) == 2
    assert sum(1 for m in v3.messages if m["role"] == "assistant") == 1


def test_failed_observation_marks_the_result():
    obs = [{"execution_number": 0, "tool_name": "read_file", "loop_index": 0,
            "tool_input": {}, "observation": {"summary": "nope", "success": False,
                                              "error": {"message": "boom"}}}]
    pi = PlannerInput(
        organization_name="Org", organization_ai_analyst_name="AI", timezone="UTC",
        user_message="go", past_observations=obs, tool_catalog=[], mode="chat",
        use_transcript=True,
    )
    v3 = PromptBuilderV3.build(pi)
    results = [b for m in v3.messages if isinstance(m["content"], list)
               for b in m["content"] if b.get("type") == "tool_result"]
    assert results and results[0].get("is_error") is True


def test_env_var_enables_the_path(monkeypatch):
    monkeypatch.setenv("BOW_PLANNER_TRANSCRIPT", "1")
    assert len(PromptBuilderV3.build(_input()).messages) > 1

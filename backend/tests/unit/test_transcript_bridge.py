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
    user_message = kw.pop("user_message", "what is total revenue?")
    return PlannerInput(
        organization_name="Org",
        organization_ai_analyst_name="AI",
        timezone="UTC",
        user_message=user_message,
        past_observations=obs,
        last_observation=obs[-1]["observation"] if obs else None,
        tool_catalog=[],
        mode="chat",
        **kw,
    )


def _blob(v3):
    return json.dumps(v3.messages, default=str)


@pytest.fixture(autouse=True)
def _no_ambient_flag(monkeypatch):
    """The flag is process-wide, so a developer sandbox that exports it would
    otherwise decide these assertions instead of the test."""
    monkeypatch.delenv("BOW_PLANNER_TRANSCRIPT", raising=False)


def test_transcript_is_the_default():
    v3 = PromptBuilderV3.build(_input())
    assert len(v3.messages) > 1


def test_kill_switch_restores_the_legacy_path(monkeypatch):
    monkeypatch.setenv("BOW_PLANNER_TRANSCRIPT", "0")
    v3 = PromptBuilderV3.build(_input())
    assert len(v3.messages) == 1
    assert isinstance(v3.messages[0]["content"], str)
    assert "<past_observations>" in v3.messages[0]["content"]


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
    import os as _os
    _prev = _os.environ.pop("BOW_PLANNER_TRANSCRIPT", None)
    _os.environ["BOW_PLANNER_TRANSCRIPT"] = "0"
    plain = _blob(PromptBuilderV3.build(_input()))
    _os.environ.pop("BOW_PLANNER_TRANSCRIPT")
    if _prev is not None:
        _os.environ["BOW_PLANNER_TRANSCRIPT"] = _prev
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


def test_env_var_explicitly_enables_the_path(monkeypatch):
    monkeypatch.setenv("BOW_PLANNER_TRANSCRIPT", "1")
    assert len(PromptBuilderV3.build(_input()).messages) > 1


# --- the live transcript wins over reconstruction -------------------------

def test_live_transcript_carries_real_provider_ids():
    """A reconstruction has to invent ids; the live transcript must not.

    Provider-opaque state (Gemini's thought_signature, which 400s if replayed
    wrong) attaches to the id the provider issued, so replaying a minted id is
    not equivalent.
    """
    from app.ai.context.transcript import Transcript
    from app.ai.context.parts import ToolCallPart, ToolResultPart

    live = Transcript()
    live.add_assistant_step(calls=[ToolCallPart(
        id="toolu_REAL_PROVIDER_ID", tool_name="read_file", args={"file_id": "a.csv"},
        signature="SIG_XYZ", provider_name="google",
    )])
    live.add_tool_results([ToolResultPart(
        call_id="toolu_REAL_PROVIDER_ID", tool_name="read_file", content="{}", tokens=1,
    )])

    pi = _input(use_transcript=True, transcript=live, provider_name="google")
    v3 = PromptBuilderV3.build(pi)
    calls = [b for m in v3.messages if isinstance(m["content"], list)
             for b in m["content"] if b.get("type") == "tool_use"]
    assert len(calls) == 1
    assert calls[0]["id"] == "toolu_REAL_PROVIDER_ID"
    assert calls[0].get("signature") == "SIG_XYZ"

    results = [b for m in v3.messages if isinstance(m["content"], list)
               for b in m["content"] if b.get("type") == "tool_result"]
    assert results[0]["tool_use_id"] == "toolu_REAL_PROVIDER_ID"


def test_live_transcript_signature_does_not_cross_providers():
    from app.ai.context.transcript import Transcript
    from app.ai.context.parts import ToolCallPart, ToolResultPart

    live = Transcript()
    live.add_assistant_step(calls=[ToolCallPart(
        id="id1", tool_name="t", signature="SIG", provider_name="google")])
    live.add_tool_results([ToolResultPart(call_id="id1", tool_name="t", content="{}", tokens=1)])

    v3 = PromptBuilderV3.build(_input(use_transcript=True, transcript=live, provider_name="anthropic"))
    calls = [b for m in v3.messages if isinstance(m["content"], list)
             for b in m["content"] if b.get("type") == "tool_use"]
    assert "signature" not in calls[0], "a signature must not follow a mid-run provider fallback"


def test_live_transcript_preferred_over_observations():
    from app.ai.context.transcript import Transcript
    from app.ai.context.parts import ToolCallPart, ToolResultPart

    live = Transcript()
    live.add_assistant_step(calls=[ToolCallPart(id="live1", tool_name="LIVE_TOOL")])
    live.add_tool_results([ToolResultPart(call_id="live1", tool_name="LIVE_TOOL", content="{}", tokens=1)])

    blob = _blob(PromptBuilderV3.build(_input(use_transcript=True, transcript=live)))
    assert "LIVE_TOOL" in blob
    assert "call_0_0" not in blob, "must not fall back to reconstructed ids"


def test_rehydrated_history_precedes_the_current_follow_up():
    """Prior-completion tools must not appear to run after the new user ask."""
    from app.ai.context.transcript import Transcript
    from app.ai.context.parts import ToolCallPart, ToolResultPart

    live = Transcript()
    live.add_assistant_step(calls=[
        ToolCallPart(id="historical-call", tool_name="inspect_data")
    ])
    live.add_tool_results([
        ToolResultPart(
            call_id="historical-call",
            tool_name="inspect_data",
            content='{"remembered_value":41}',
            tokens=6,
        )
    ])
    live.history_turn_count = len(live.turns)

    v3 = PromptBuilderV3.build(
        _input(
            user_message="CURRENT_FOLLOW_UP_MARKER",
            use_transcript=True,
            transcript=live,
        )
    )
    blob = _blob(v3)
    assert blob.index("remembered_value") < blob.index("CURRENT_FOLLOW_UP_MARKER")


# --- budget resolution ---------------------------------------------------

def test_budget_defaults_to_half_the_window():
    from app.ai.agents.planner.transcript_bridge import transcript_budget_tokens
    pi = _input(context_window_tokens=200_000)
    assert transcript_budget_tokens(pi) == 100_000


def test_null_window_does_not_disable_decay():
    """A model row with no context_window_tokens previously produced budget 0,
    and the caller's `window > 0` guard then skipped decay ENTIRELY — observed
    on an Azure deployment. Assume a small window instead of assuming none."""
    from app.ai.agents.planner.transcript_bridge import transcript_budget_tokens
    pi = _input(context_window_tokens=None)
    budget = transcript_budget_tokens(pi)
    assert budget > 0
    assert budget <= 128_000


def test_ratio_override(monkeypatch):
    from app.ai.agents.planner.transcript_bridge import transcript_budget_tokens
    monkeypatch.setenv("BOW_TRANSCRIPT_BUDGET_RATIO", "0.01")
    assert transcript_budget_tokens(_input(context_window_tokens=200_000)) == 2_000


def test_absolute_override_wins(monkeypatch):
    from app.ai.agents.planner.transcript_bridge import transcript_budget_tokens
    monkeypatch.setenv("BOW_TRANSCRIPT_BUDGET_RATIO", "0.5")
    monkeypatch.setenv("BOW_TRANSCRIPT_BUDGET_TOKENS", "777")
    assert transcript_budget_tokens(_input(context_window_tokens=200_000)) == 777


def test_tiny_budget_actually_decays_the_rendered_messages(monkeypatch):
    monkeypatch.setenv("BOW_TRANSCRIPT_BUDGET_TOKENS", "1")
    v3 = PromptBuilderV3.build(_input(obs_count=6, use_transcript=True))
    results = [b for m in v3.messages if isinstance(m["content"], list)
               for b in m["content"] if b.get("type") == "tool_result"]
    assert results, "results must still be present after decay"
    # The oldest results carry the 500-char `details` payload when FULL; after
    # decay they must be shorter than that.
    assert min(len(r["content"]) for r in results) < 400
    # …and every call still has its result.
    calls = {b["id"] for m in v3.messages if isinstance(m["content"], list)
             for b in m["content"] if b.get("type") == "tool_use"}
    got = {b["tool_use_id"] for m in v3.messages if isinstance(m["content"], list)
           for b in m["content"] if b.get("type") == "tool_result"}
    assert calls == got, "decay must not orphan a call"


def test_static_context_excludes_volatile_conversation_history():
    """Turn 0 must be byte-stable within a run or no cache breakpoint can hit.

    messages_context is rebuilt every iteration and grows as the agent's own
    completion blocks accumulate, so it belongs in the volatile tail.
    """
    pi = _input(use_transcript=True, messages_context="<conversation>CONV_MARKER</conversation>")
    v3 = PromptBuilderV3.build(pi)
    first = json.dumps(v3.messages[0], default=str)
    assert "CONV_MARKER" not in first, "history must not sit in the cacheable head"
    assert "CONV_MARKER" in _blob(v3), "…but it must still reach the model"


def test_static_context_keeps_the_stable_blocks():
    pi = _input(use_transcript=True)
    first = json.dumps(PromptBuilderV3.build(pi).messages[0], default=str)
    assert "rule one" in first, "instructions belong in the cacheable head"
    assert "sales" in first, "schemas belong in the cacheable head"


def test_knowledge_mode_never_uses_the_transcript(monkeypatch):
    """The knowledge harness is single-shot: no prior steps to replay, and its
    ask lives in _build_knowledge_user_message rather than user_message. On the
    transcript path that message is discarded and messages[0] becomes the
    generic static context — so the harness's instructions silently never reach
    the model while the system prompt still looks correct.
    """
    from app.ai.agents.planner import transcript_bridge
    from app.schemas.ai.planner import PlannerInput

    monkeypatch.setenv("BOW_PLANNER_TRANSCRIPT", "1")
    assert transcript_bridge.enabled(PlannerInput(user_message="x", mode="knowledge")) is False
    # ...and an explicit opt-in must not override it either.
    assert transcript_bridge.enabled(
        PlannerInput(user_message="x", mode="knowledge", use_transcript=True)) is False
    # Every other mode still gets the transcript.
    assert transcript_bridge.enabled(PlannerInput(user_message="x", mode="chat")) is True

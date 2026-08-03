"""Transcript, decay ladder, and the render seam.

Asserts the invariants the design depends on rather than any provider envelope.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest  # noqa: E402

from app.ai.context.parts import (  # noqa: E402
    Outcome, TextPart, ThinkingPart, Tier, ToolCallPart, ToolResultPart, estimate_tokens,
)
from app.ai.context.transcript import Transcript  # noqa: E402


def _result(cid, name="read_file", content="x" * 400, digest="short digest", **kw):
    return ToolResultPart(
        call_id=cid, tool_name=name, content=content, digest=digest,
        tokens=estimate_tokens(content), **kw,
    )


def _built(n_steps=6):
    t = Transcript()
    t.add_user_text("what is total revenue?")
    for i in range(n_steps):
        t.add_assistant_step(calls=[ToolCallPart(id=f"c{i}", tool_name="read_file", args={"f": i})])
        t.add_tool_results([_result(f"c{i}")])
    return t


# --- shape ---------------------------------------------------------------

def test_grows_instead_of_rebuilding():
    t = _built(3)
    msgs = t.to_model_messages()
    assert len(msgs) > 1, "transcript must produce a multi-turn conversation"
    assert any(isinstance(m.content, list) for m in msgs), "expected block content"


def test_results_render_as_tool_result_blocks():
    t = _built(1)
    blocks = [b for m in t.to_model_messages() if isinstance(m.content, list) for b in m.content]
    assert any(b["type"] == "tool_use" for b in blocks)
    assert any(b["type"] == "tool_result" for b in blocks)


def test_parallel_batch_is_one_user_turn():
    t = Transcript()
    t.add_assistant_step(calls=[
        ToolCallPart(id="a", tool_name="x"), ToolCallPart(id="b", tool_name="y"),
    ])
    t.add_tool_results([_result("a"), _result("b")])
    msgs = t.to_model_messages()
    results = [b for m in msgs if isinstance(m.content, list)
               for b in m.content if b["type"] == "tool_result"]
    assert len(results) == 2
    assert sum(1 for m in msgs if m.role == "user") == 1


def test_head_text_rides_with_results():
    t = Transcript()
    t.add_assistant_step(calls=[ToolCallPart(id="a", tool_name="x")])
    t.add_tool_results([_result("a")], head_text="<time>now</time>")
    blocks = [b for m in t.to_model_messages() if isinstance(m.content, list) for b in m.content]
    assert any(b["type"] == "tool_result" for b in blocks)
    assert any(b.get("text") == "<time>now</time>" for b in blocks)


# --- outcomes ------------------------------------------------------------

@pytest.mark.parametrize("outcome", [Outcome.FAILED, Outcome.DENIED, Outcome.INTERRUPTED])
def test_non_success_is_marked(outcome):
    t = Transcript()
    t.add_assistant_step(calls=[ToolCallPart(id="a", tool_name="x")])
    t.add_tool_results([_result("a", outcome=outcome, content="boom")])
    blk = [b for m in t.to_model_messages() if isinstance(m.content, list)
           for b in m.content if b["type"] == "tool_result"][0]
    assert blk.get("is_error") is True
    assert outcome.value in blk["content"], "outcome must be legible in the text too"


def test_orphaned_call_is_repaired_not_dropped():
    t = Transcript()
    t.add_assistant_step(calls=[ToolCallPart(id="ghost", tool_name="x")])
    synthesized = t.repair()
    assert len(synthesized) == 1
    assert synthesized[0].outcome is Outcome.INTERRUPTED
    calls, results = set(), set()
    for turn in t.turns:
        calls |= turn.tool_call_ids()
        results |= turn.tool_result_ids()
    assert calls == results, "every call must have a matching result after repair"


def test_repair_is_idempotent():
    t = Transcript()
    t.add_assistant_step(calls=[ToolCallPart(id="ghost", tool_name="x")])
    assert len(t.repair()) == 1
    assert t.repair() == []


# --- the ladder ----------------------------------------------------------

def test_decay_reduces_size_and_keeps_pairs_intact():
    t = _built(8)
    before = t.tokens()
    stats = t.fit_to_budget(int(before * 0.35))
    assert t.tokens() < before
    assert stats["digested"] > 0
    calls, results = set(), set()
    for turn in t.turns:
        calls |= turn.tool_call_ids()
        results |= turn.tool_result_ids()
    assert calls == results, "decay must never orphan a call"


def test_recent_turns_are_never_decayed():
    t = _built(8)
    t.fit_to_budget(1)
    tail = t.turns[-4:]
    tail_results = [p for turn in tail for p in turn.parts if isinstance(p, ToolResultPart)]
    assert tail_results, "sanity: tail should hold results"
    assert all(p.tier is Tier.FULL for p in tail_results)


def test_digest_tier_costs_no_llm_call_and_keeps_meaning():
    p = _result("a", content="x" * 4000, digest="42 rows x 5 cols")
    p.tier = Tier.DIGEST
    assert p.model_text() == "42 rows x 5 cols"


def test_digest_falls_back_to_content_when_absent():
    p = _result("a", content="body text", digest=None)
    p.tier = Tier.DIGEST
    assert p.model_text() == "body text", "decay must not blank a result with no digest"


def test_within_budget_is_a_noop():
    t = _built(3)
    before = t.tokens()
    stats = t.fit_to_budget(before * 10)
    assert stats["digested"] == 0 and stats["dropped"] == 0
    assert stats["before"] == before and stats["after"] == before
    assert stats["reached"] is True


# --- the seam ------------------------------------------------------------

def test_metadata_never_reaches_the_model():
    t = Transcript()
    t.add_assistant_step(calls=[ToolCallPart(id="a", tool_name="x")])
    t.add_tool_results([_result("a", metadata={"secret_internal": "NEVER_SEND"})])
    import json
    assert "NEVER_SEND" not in json.dumps(
        [m.content for m in t.to_model_messages()], default=str
    )


def test_signature_replays_only_to_issuing_provider():
    t = Transcript()
    t.add_assistant_step(calls=[
        ToolCallPart(id="a", tool_name="x", signature="SIG", provider_name="google"),
    ])
    t.add_tool_results([_result("a")])

    same = [b for m in t.to_model_messages(provider_name="google")
            if isinstance(m.content, list) for b in m.content if b["type"] == "tool_use"]
    assert same[0].get("signature") == "SIG"

    other = [b for m in t.to_model_messages(provider_name="anthropic")
             if isinstance(m.content, list) for b in m.content if b["type"] == "tool_use"]
    assert "signature" not in other[0], "a signature must not cross providers"


def test_thinking_is_dropped_across_providers():
    t = Transcript()
    t.add_assistant_step(
        thinking=ThinkingPart(text="SECRET_REASONING", signature="S", provider_name="anthropic"),
        calls=[ToolCallPart(id="a", tool_name="x")],
    )
    import json
    blob = json.dumps([m.content for m in t.to_model_messages(provider_name="openai")], default=str)
    assert "SECRET_REASONING" not in blob


def test_oversized_result_is_truncated_with_a_notice():
    t = Transcript()
    t.add_assistant_step(calls=[ToolCallPart(id="a", tool_name="x")])
    t.add_tool_results([_result("a", content="y" * 5000, digest=None)])
    blk = [b for m in t.to_model_messages(max_result_chars=1000)
           if isinstance(m.content, list) for b in m.content if b["type"] == "tool_result"][0]
    assert len(blk["content"]) < 5000
    assert "truncated" in blk["content"]


def test_images_are_hoisted_beside_the_result():
    t = Transcript()
    t.add_assistant_step(calls=[ToolCallPart(id="a", tool_name="x")])
    t.add_tool_results([_result("a", images=[{"type": "base64", "media_type": "image/png", "data": "IMG"}])])
    blocks = [b for m in t.to_model_messages() if isinstance(m.content, list) for b in m.content]
    assert any(b["type"] == "image" for b in blocks)


def test_images_omitted_when_model_lacks_vision():
    t = Transcript()
    t.add_assistant_step(calls=[ToolCallPart(id="a", tool_name="x")])
    t.add_tool_results([_result("a", images=[{"type": "base64", "media_type": "image/png", "data": "IMG"}])])
    blocks = [b for m in t.to_model_messages(supports_images=False)
              if isinstance(m.content, list) for b in m.content]
    assert not any(b["type"] == "image" for b in blocks)


def test_dropped_result_is_not_mistakable_for_failure():
    """Elision must announce itself.

    An empty body made live models answer "no row count returned (file may not
    exist or error occurred)" for a call that had succeeded — the gap read as a
    failure. A dropped result must still state the outcome and name the tool.
    """
    p = _result("a", name="get_row_count", content="x" * 4000, digest=None)
    p.tier = Tier.DROPPED
    body = p.model_text()
    assert body, "a dropped result must not render as an empty string"
    assert "get_row_count" in body
    assert "success" in body.lower()
    assert "elided" in body.lower()
    assert not body.startswith("[failed"), "must not look like an error"


def test_dropped_failure_still_reads_as_failure():
    p = _result("a", content="boom", digest=None, outcome=Outcome.FAILED)
    p.tier = Tier.DROPPED
    assert Outcome.FAILED.value in p.model_text()


def test_reports_when_budget_is_unreachable():
    """A budget below the non-decayable floor cannot be met.

    Observed live: a 1200-token budget moved a real transcript 2596 -> 2593
    tokens and returned as if it had succeeded. The excess was static context
    (a TextPart the ladder cannot touch), so the caller needed to know decay
    was not the fix.
    """
    t = Transcript()
    t.add_user_text("S" * 8000)  # static context — not decayable
    for i in range(6):
        t.add_assistant_step(calls=[ToolCallPart(id=f"c{i}", tool_name="read_file")])
        t.add_tool_results([_result(f"c{i}")])

    stats = t.fit_to_budget(10)
    assert stats["reached"] is False
    assert stats["floor"] >= 2000, "floor must account for the undecayable text"
    assert stats["after"] >= stats["floor"]


def test_reached_is_true_when_budget_is_met():
    t = _built(8)
    stats = t.fit_to_budget(int(t.tokens() * 0.8))
    assert stats["reached"] is True
    assert stats["after"] <= int(stats["before"] * 0.8)


def test_floor_shrinks_as_results_decay():
    t = _built(8)
    floor_before = t.floor_tokens()
    assert floor_before < t.tokens(), "some of the transcript must be reducible"

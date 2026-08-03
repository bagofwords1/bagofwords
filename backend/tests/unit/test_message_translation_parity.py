"""Cross-provider parity for Message -> provider translation.

Every client must carry the same semantic content. These are the invariants a
growing transcript depends on: a user turn that holds tool results AND text
must deliver both, several text blocks must not collapse to the first, and
image blocks must survive in a block-shaped message.

Contract-level: each test asserts "the content reached the request", not the
exact provider envelope, so the shapes can change without breaking these.
"""
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest  # noqa: E402

from app.ai.llm.clients.anthropic_client import Anthropic  # noqa: E402
from app.ai.llm.clients.azure_client import AzureClient  # noqa: E402
from app.ai.llm.clients.openai_client import OpenAi  # noqa: E402
from app.ai.llm.clients.openai_responses_client import OpenAIResponsesClient  # noqa: E402
from app.ai.llm.types import Message  # noqa: E402

# (label, translate callable). Google/Bedrock build SDK objects rather than
# plain dicts, so they are covered by their own dedicated tests.
TRANSLATORS = [
    ("anthropic", Anthropic._translate_messages),
    ("openai", OpenAi._translate_messages),
    ("azure", AzureClient._translate_messages),
    ("responses", OpenAIResponsesClient._translate_messages),
]


def _blob(translated) -> str:
    """Flatten a translated request to text so parity can be asserted without
    coupling to any one provider's envelope."""
    return json.dumps(translated, default=str)


@pytest.mark.parametrize("label,translate", TRANSLATORS)
def test_text_alongside_tool_results_is_not_dropped(label, translate):
    """A user turn carrying tool results AND text must deliver both.

    This is exactly the shape the transcript produces: the results of the last
    step plus the per-turn head (clock, steering, nudges). Dropping the text is
    silent — the model simply never sees the steer.
    """
    messages = [
        Message(role="user", content=[
            {"type": "tool_result", "tool_use_id": "call_0", "content": "42 rows"},
            {"type": "text", "text": "STEERING_MARKER: only look at Q3"},
        ]),
    ]
    out = _blob(translate(messages))
    assert "42 rows" in out, f"{label}: tool result lost"
    assert "STEERING_MARKER" in out, f"{label}: text alongside tool_result was dropped"


@pytest.mark.parametrize("label,translate", TRANSLATORS)
def test_all_text_blocks_survive(label, translate):
    messages = [
        Message(role="assistant", content=[
            {"type": "text", "text": "FIRST_BLOCK"},
            {"type": "text", "text": "SECOND_BLOCK"},
            {"type": "tool_use", "id": "call_0", "name": "t", "input": {}},
        ]),
    ]
    out = _blob(translate(messages))
    assert "FIRST_BLOCK" in out, f"{label}: first text block lost"
    assert "SECOND_BLOCK" in out, f"{label}: second text block dropped"


@pytest.mark.parametrize("label,translate", TRANSLATORS)
def test_image_blocks_survive_in_block_messages(label, translate):
    """A tool result that produced a screenshot must still reach a vision model
    when the message is block-shaped rather than a plain string."""
    messages = [
        Message(role="user", content=[
            {"type": "text", "text": "what does this show?"},
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": "IMGDATA_MARKER",
            }},
        ]),
    ]
    out = _blob(translate(messages))
    assert "what does this show?" in out, f"{label}: text lost"
    assert "IMGDATA_MARKER" in out, f"{label}: image block dropped"


@pytest.mark.parametrize("label,translate", TRANSLATORS)
def test_tool_call_and_result_round_trip(label, translate):
    messages = [
        Message(role="user", content="how many rows?"),
        Message(role="assistant", content=[
            {"type": "tool_use", "id": "call_0", "name": "get_rows", "input": {"file": "sales.csv"}},
        ]),
        Message(role="user", content=[
            {"type": "tool_result", "tool_use_id": "call_0", "content": '{"rows": 200}'},
        ]),
    ]
    out = _blob(translate(messages))
    assert "get_rows" in out, f"{label}: tool name lost"
    assert "sales.csv" in out, f"{label}: tool arguments lost"
    assert "200" in out, f"{label}: tool result lost"


@pytest.mark.parametrize("label,translate", TRANSLATORS)
def test_error_results_are_marked(label, translate):
    messages = [
        Message(role="user", content=[
            {"type": "tool_result", "tool_use_id": "c0", "content": "boom", "is_error": True},
        ]),
    ]
    out = _blob(translate(messages))
    assert "boom" in out, f"{label}: error result content lost"


# ── parallel batch ─────────────────────────────────────────────────────────
# ai_tool_concurrency defaults to 4, so the common shape is N tool_use blocks
# in ONE assistant turn answered by N tool_result blocks in ONE user turn. The
# single-call cases above never exercise pairing, which is where a translator
# that flattens or reorders does damage that no exception reports.

def _batch_messages(n: int):
    return [
        Message(role="user", content="run the batch"),
        Message(role="assistant", content=(
            [{"type": "text", "text": "Running them together."}]
            + [
                {"type": "tool_use", "id": f"call_{i}", "name": f"tool_{i}",
                 "input": {"idx": i}}
                for i in range(n)
            ]
        )),
        Message(role="user", content=[
            {"type": "tool_result", "tool_use_id": f"call_{i}",
             "content": f"RESULT_{i}"}
            for i in range(n)
        ]),
    ]


@pytest.mark.parametrize("label,translate", TRANSLATORS)
def test_parallel_batch_keeps_every_call_and_result(label, translate):
    """No member of a batch may vanish in translation."""
    out = _blob(translate(_batch_messages(4)))
    for i in range(4):
        assert f"call_{i}" in out, f"{label}: tool_use call_{i} lost from the batch"
        assert f"RESULT_{i}" in out, f"{label}: tool_result for call_{i} lost"


@pytest.mark.parametrize("label,translate", TRANSLATORS)
def test_parallel_batch_keeps_narration(label, translate):
    """Assistant text riding with the tool_use blocks is the model's own
    narration — the transcript replays it so the next turn knows what it said."""
    out = _blob(translate(_batch_messages(3)))
    assert "Running them together." in out, f"{label}: assistant narration dropped"


@pytest.mark.parametrize("label,translate", TRANSLATORS)
def test_batch_result_ids_match_their_calls(label, translate):
    """Every tool_result must carry the id of a call in the same batch.

    A translator that renumbers or reuses an index silently reattaches results
    to the wrong call: the model then reads tool_2's output as tool_0's, and
    nothing errors.
    """
    blob = _blob(translate(_batch_messages(4)))
    for i in range(4):
        # The id has to appear at least twice — once on the call, once on the
        # result that answers it.
        assert blob.count(f"call_{i}") >= 2, (
            f"{label}: call_{i} appears {blob.count(f'call_{i}')}x — "
            "its result is not keyed to it"
        )


@pytest.mark.parametrize("label,translate", TRANSLATORS)
def test_single_call_batch_is_not_special_cased(label, translate):
    """n=1 goes down the same path as n=4 — a batch of one is still a batch."""
    out = _blob(translate(_batch_messages(1)))
    assert "call_0" in out and "RESULT_0" in out, f"{label}: single-call batch broken"

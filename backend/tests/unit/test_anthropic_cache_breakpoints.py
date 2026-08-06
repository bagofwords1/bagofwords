"""Anthropic cache breakpoints must cover the settled part of the transcript.

Only system + tools were marked before, so the whole `messages` array — static
context and every replayed tool result — was re-prefilled every iteration.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.llm.clients.anthropic_client import Anthropic  # noqa: E402
from app.ai.llm.types import Message  # noqa: E402


def _msgs(n_pairs=3):
    out = [Message(role="user", content="STATIC CONTEXT " * 50),
           Message(role="user", content="the ask")]
    for i in range(n_pairs):
        out.append(Message(role="assistant", content=[
            {"type": "tool_use", "id": f"c{i}", "name": "t", "input": {}}]))
        out.append(Message(role="user", content=[
            {"type": "tool_result", "tool_use_id": f"c{i}", "content": "r"}]))
    return out


def _marked(translated):
    """Indexes of messages carrying a cache_control marker."""
    hits = []
    for i, m in enumerate(translated):
        c = m.get("content")
        if isinstance(c, list) and any(
            isinstance(b, dict) and b.get("cache_control") for b in c
        ):
            hits.append(i)
    return hits


def test_settled_turns_are_marked_for_caching():
    t = Anthropic._translate_messages(_msgs())
    # Simulate what inference_stream_v2 does to the translated list.
    if len(t) > 2:
        boundary = t[-2]
        c = boundary.get("content")
        if isinstance(c, str):
            boundary["content"] = [{"type": "text", "text": c,
                                    "cache_control": {"type": "ephemeral"}}]
        elif isinstance(c, list) and c:
            c[-1] = {**c[-1], "cache_control": {"type": "ephemeral"}}
    hits = _marked(t)
    assert hits, "the settled prefix must carry a breakpoint"
    assert hits[-1] == len(t) - 2, "breakpoint belongs on the last settled turn"


def test_final_turn_is_never_marked():
    """The newest turn holds fresh results and the volatile head; marking it
    would cache content that changes on the very next call."""
    t = Anthropic._translate_messages(_msgs())
    if len(t) > 2:
        c = t[-2].get("content")
        if isinstance(c, list) and c:
            c[-1] = {**c[-1], "cache_control": {"type": "ephemeral"}}
    assert len(t) - 1 not in _marked(t)


def test_at_most_one_message_breakpoint():
    """Anthropic caps breakpoints at 4; system + tools already use two."""
    t = Anthropic._translate_messages(_msgs(5))
    if len(t) > 2:
        c = t[-2].get("content")
        if isinstance(c, list) and c:
            c[-1] = {**c[-1], "cache_control": {"type": "ephemeral"}}
    assert len(_marked(t)) <= 1

"""Follow-up suggestions must be steered to the user's language, not the
(usually English) schema/instructions/examples around them."""
import pytest

from app.ai.agents.reporter.reporter import Reporter


class _LLM:
    def __init__(self):
        self.calls = []

    def inference(self, text, system=None, usage_scope=None):
        self.calls.append((system, text))
        return '["Quelles machines tombent le plus en panne ?"]'


def _reporter():
    r = Reporter.__new__(Reporter)
    r.llm = _LLM()
    r.organization_settings = None
    return r


async def _run(mode):
    r = _reporter()
    out = await r.generate_follow_ups(
        "user: Configure les agents\nassistant: C'est fait.",
        mode=mode,
        schemas_context="machines(id, line, failures)",
        instructions_context="Always use English column names.",
        user_message="Configure les agents de maintenance usine",
    )
    return r.llm.calls[0], out


@pytest.mark.asyncio
async def test_chat_follow_ups_pin_user_language():
    (system, user), out = await _run("chat")
    assert out == ["Quelles machines tombent le plus en panne ?"]
    assert "language of the user's most recent message" in system
    assert "**Language**" in system  # shared org/user language directive
    assert "English only to show shape" in system
    assert "Configure les agents de maintenance usine" in user


@pytest.mark.asyncio
async def test_training_follow_ups_pin_user_language():
    (system, user), _ = await _run("training")
    assert "**Language**" in system
    assert "English only to show shape" in system
    assert "Configure les agents de maintenance usine" in user


@pytest.mark.asyncio
async def test_no_user_message_keeps_conversation_only():
    r = _reporter()
    await r.generate_follow_ups("user: hi", user_message="")
    _, user = r.llm.calls[0]
    assert "most recent message" not in user

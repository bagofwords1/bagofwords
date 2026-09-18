"""The run-invariant prefix (tools + system) must use the 1-hour cache TTL.

Anthropic's default ephemeral TTL is 5 minutes. Inside one agent run the planner
iterates fast enough to keep the entry warm, but between user turns a person
reads the answer and types the next question — routinely longer than that — so
the whole tools+system prefix was re-written at 1.25x on the first call of
nearly every turn instead of being read back at 0.1x.

The per-turn message breakpoint must NOT get the long TTL: it moves every
iteration, so a 2x write would buy an entry that is superseded seconds later.
Anthropic also requires longer-TTL entries to appear before shorter-TTL ones,
and the render order is tools -> system -> messages, so 1h/1h/5m is valid.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest  # noqa: E402

from app.ai.llm.clients.anthropic_client import _prefix_cache_control  # noqa: E402


def test_prefix_uses_one_hour_ttl_by_default(monkeypatch):
    monkeypatch.delenv("BOW_PROMPT_CACHE_TTL", raising=False)
    assert _prefix_cache_control() == {"type": "ephemeral", "ttl": "1h"}


@pytest.mark.parametrize("value", ["5m", "5min", "default", "ephemeral", "5M", " 5m "])
def test_env_override_restores_five_minute_default(monkeypatch, value):
    """A one-env-var rollback, no deploy needed."""
    monkeypatch.setenv("BOW_PROMPT_CACHE_TTL", value)
    assert _prefix_cache_control() == {"type": "ephemeral"}


def test_unrecognised_value_falls_back_to_one_hour(monkeypatch):
    monkeypatch.setenv("BOW_PROMPT_CACHE_TTL", "banana")
    assert _prefix_cache_control() == {"type": "ephemeral", "ttl": "1h"}


def test_message_breakpoint_keeps_the_short_ttl(monkeypatch):
    """The moving per-turn breakpoint must stay on the 5-minute default, and it
    must sort AFTER the prefix (tools -> system -> messages) so the longer TTL
    always precedes the shorter one."""
    monkeypatch.delenv("BOW_PROMPT_CACHE_TTL", raising=False)
    from app.ai.llm.clients import anthropic_client as ac
    src = Path(ac.__file__).read_text()
    # The messages breakpoint is written literally, not via _prefix_cache_control.
    assert 'content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}' in src
    assert "ttl" not in src.split("boundary = msgs[-2]")[1].split("if system:")[0]

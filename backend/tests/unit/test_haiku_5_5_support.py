"""Claude Haiku 5.5 request shaping: no sampling params, adaptive thinking, standard cache rates."""
import pytest

from app.ai.agent_v2 import _effort_to_thinking_config
from app.ai.llm import pricing
from app.ai.llm.clients.anthropic_client import _accepts_temperature
from app.ai.llm.reasoning import claude_off_params, native_efforts
from app.models.llm_model import LLM_MODEL_DETAILS


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_haiku_5_5_never_gets_budget_tokens(effort):
    cfg = _effort_to_thinking_config(effort, "claude-haiku-5-5")
    assert cfg["type"] == "adaptive"
    assert "budget_tokens" not in cfg
    assert cfg["effort"] == effort


def test_haiku_5_5_offers_full_effort_range():
    assert tuple(native_efforts("claude-haiku-5-5")) == ("low", "medium", "high", "xhigh", "max")
    # Haiku 4.5 stays budget-only.
    assert "xhigh" not in native_efforts("claude-haiku-4-5")


def test_haiku_5_5_off_disables_at_low_effort():
    # disabled is accepted only at effort high or below.
    assert claude_off_params("claude-haiku-5-5") == {
        "thinking": {"type": "disabled"}, "output_config": {"effort": "low"},
    }


def test_haiku_5_5_rejects_temperature():
    assert _accepts_temperature("claude-haiku-5-5") is False
    assert _accepts_temperature("claude-haiku-4-5-20251001") is True


def test_haiku_5_5_standard_cache_rates():
    rates = pricing.rates_for("anthropic", "claude-haiku-5-5")
    assert rates.read == pytest.approx(0.1)


def test_haiku_5_5_catalog_entry():
    entry = next(m for m in LLM_MODEL_DETAILS if m["model_id"] == "claude-haiku-5-5")
    assert entry["provider_type"] == "anthropic"
    assert entry["supports_vision"] is True
    assert entry["context_window_tokens"] == 1_000_000
    assert entry["max_output_tokens"] == 128_000
    assert entry["input_cost_per_million_tokens_usd"] == 0.10
    assert entry["output_cost_per_million_tokens_usd"] == 0.50
    assert entry["is_default"] is False
    assert not entry.get("is_small_default")

"""Claude Opus 5.5 request shaping: no sampling params, adaptive thinking only."""
import pytest

from app.ai.agent_v2 import _effort_to_thinking_config
from app.ai.llm.clients.anthropic_client import _accepts_temperature
from app.models.llm_model import LLM_MODEL_DETAILS


@pytest.mark.parametrize("model_id", ["claude-opus-5-5", "claude-opus-5"])
@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_opus_5_family_never_gets_budget_tokens(model_id, effort):
    # budget_tokens is a 400 on both; adaptive is the only on-mode.
    assert _effort_to_thinking_config(effort, model_id) == {"type": "adaptive"}


def test_opus_5_5_effort_off_omits_thinking():
    # Sending {"type": "disabled"} is a 400 on Opus 5.5; omitting runs adaptive.
    assert _effort_to_thinking_config("off", "claude-opus-5-5") is None


def test_opus_5_5_rejects_temperature():
    assert _accepts_temperature("claude-opus-5-5") is False


def test_opus_5_5_catalog_entry():
    entry = next(m for m in LLM_MODEL_DETAILS if m["model_id"] == "claude-opus-5-5")
    assert entry["provider_type"] == "anthropic"
    assert entry["context_window_tokens"] == 1_000_000
    assert entry["max_output_tokens"] == 128_000
    assert entry["input_cost_per_million_tokens_usd"] == 4.00
    assert entry["output_cost_per_million_tokens_usd"] == 20.00
    assert entry["is_default"] is False

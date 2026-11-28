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


# --- prompt-length pricing --------------------------------------------------
# Over 100K prompt tokens (cache reads and writes included) every category
# bills at 5x: $0.50/$2.50 input/output, $0.05 cache read, per Anthropic's
# pricing page. At exactly 100K it is still the base tier.

def _input(prompt, read=0, write_5m=0, model="claude-haiku-5-5"):
    return pricing.cached_input_cost(
        rate_per_million=0.10, prompt_tokens=prompt, cache_read_tokens=read,
        cache_write_5m_tokens=write_5m, cache_write_1h_tokens=0,
        provider_type="anthropic", model_id=model,
    )


def test_haiku_5_5_base_tier_up_to_100k():
    assert _input(100_000) == pytest.approx(0.01)
    assert pricing.long_context_multipliers("claude-haiku-5-5", 100_000) == (1.0, 1.0)


def test_haiku_5_5_long_prompt_bills_5x_on_input_and_output():
    assert _input(1_000_000) == pytest.approx(0.50)
    assert pricing.long_context_multipliers("claude-haiku-5-5", 100_001) == (5.0, 5.0)


def test_haiku_5_5_cache_tokens_count_toward_the_threshold():
    # 10K uncached + 95K read = 105K prompt: long tier, reads at $0.05/MTok.
    cost = _input(10_000, read=95_000)
    assert cost == pytest.approx(10_000 * 0.50 / 1e6 + 95_000 * 0.05 / 1e6)
    assert pricing.long_context_multipliers("claude-haiku-5-5", 10_000, 95_000) == (5.0, 5.0)
    assert pricing.long_context_multipliers("claude-haiku-5-5", 10_000, 0, 95_000) == (5.0, 5.0)


def test_haiku_5_5_long_prompt_applies_to_output_cost():
    from types import SimpleNamespace
    from app.services.llm_usage_recorder import LLMUsageRecorderService
    model = SimpleNamespace(model_id="claude-haiku-5-5", get_output_cost_rate=lambda: 0.50)
    short = LLMUsageRecorderService._calc_output_cost(model, 1_000_000, 50_000)
    long = LLMUsageRecorderService._calc_output_cost(model, 1_000_000, 50_000, cache_read_tokens=60_000)
    assert short == pytest.approx(0.50)
    assert long == pytest.approx(2.50)


@pytest.mark.parametrize("model", ["claude-haiku-4-5-20251001", "claude-sonnet-5-5", "claude-opus-5-5"])
def test_other_claude_models_have_no_prompt_length_tier(model):
    assert pricing.long_context_multipliers(model, 900_000) == (1.0, 1.0)

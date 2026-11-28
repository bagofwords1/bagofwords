"""Gemini catalog entries and Gemini 3.1 Pro's long-prompt price tier."""
import pytest

from app.ai.llm import pricing
from app.ai.llm.clients.google_client import GEMINI_LEVEL_ONLY_PREFIXES
from app.ai.llm.reasoning import native_efforts
from app.models.llm_model import LLM_MODEL_DETAILS

# (model_id, input $/MTok, output $/MTok) at Google's standard list price.
NEW_GOOGLE_MODELS = [
    ("gemini-4-neon", 3.00, 15.00),
    ("gemini-4-carbon", 4.00, 20.00),
    ("gemini-4-hydrogen", 1.50, 7.50),
    ("gemini-3.8-flash", 1.50, 7.50),
]


def _google(model_id):
    return next(m for m in LLM_MODEL_DETAILS if m["model_id"] == model_id and m["provider_type"] == "google")


@pytest.mark.parametrize("model_id,inp,out", NEW_GOOGLE_MODELS)
def test_new_gemini_models_are_selectable_presets_not_defaults(model_id, inp, out):
    entry = _google(model_id)
    assert entry["is_preset"] and entry["is_enabled"]
    assert not entry.get("is_default") and not entry.get("is_small_default")
    assert entry["supports_vision"] is True
    assert entry["context_window_tokens"] == 1048576
    assert entry["input_cost_per_million_tokens_usd"] == inp
    assert entry["output_cost_per_million_tokens_usd"] == out


@pytest.mark.parametrize("model_id", ["gemini-4-neon", "gemini-4-carbon", "gemini-4-hydrogen"])
def test_gemini_4_models_use_thinking_levels(model_id):
    assert model_id.startswith(GEMINI_LEVEL_ONLY_PREFIXES)
    assert tuple(native_efforts(model_id)) == ("low", "medium", "high")


def test_google_defaults_unchanged():
    google = [m for m in LLM_MODEL_DETAILS if m["provider_type"] == "google"]
    assert [m["model_id"] for m in google if m.get("is_default")] == ["gemini-3.6-flash"]
    assert [m["model_id"] for m in google if m.get("is_small_default")] == ["gemini-3.5-flash-lite"]


def _input(prompt, model="gemini-3.1-pro-preview"):
    return pricing.cached_input_cost(
        rate_per_million=2.00, prompt_tokens=prompt, cache_read_tokens=0,
        cache_write_5m_tokens=0, cache_write_1h_tokens=0,
        provider_type="google", model_id=model,
    )


def test_gemini_3_1_pro_base_tier_up_to_200k():
    assert _input(200_000) == pytest.approx(0.40)
    assert pricing.long_context_multipliers("gemini-3.1-pro-preview", 200_000) == (1.0, 1.0)


def test_gemini_3_1_pro_long_prompt_is_4_and_18():
    # $2.00 -> $4.00 input, $12.00 -> $18.00 output above 200K prompt tokens.
    assert _input(1_000_000) == pytest.approx(4.00)
    assert pricing.long_context_multipliers("gemini-3.1-pro-preview", 200_001) == (2.0, 1.5)
    assert 12.00 * 1.5 == pytest.approx(18.00)


def test_gemini_3_1_pro_tier_applies_on_vertex_too():
    assert pricing.long_context_multipliers("gemini-3.1-pro-preview", 300_000) == (2.0, 1.5)


@pytest.mark.parametrize("model", ["gemini-4-neon", "gemini-4-argon", "gemini-3.8-flash", "gemini-3.6-flash"])
def test_other_gemini_models_have_no_prompt_length_tier(model):
    assert pricing.long_context_multipliers(model, 900_000) == (1.0, 1.0)

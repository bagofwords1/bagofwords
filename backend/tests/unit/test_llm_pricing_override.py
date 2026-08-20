"""Admin-set model pricing must survive catalog re-syncs.

The models-list endpoint syncs every catalog-type provider on each load and
refreshes preset rows from LLM_MODEL_DETAILS. Pricing set through the pricing
endpoint is stored as an override that wins over the catalog value, mirroring
supports_vision_override / context_window_tokens_override.
"""
import app.models  # noqa: F401  (register every mapper before instantiating)
from app.models.llm_model import LLMModel
from app.services.llm_service import LLMService


CATALOG_ENTRY = {
    "name": "Claude 4.5 Haiku",
    "model_id": "claude-haiku-4-5-20251001",
    "is_preset": True,
    "is_enabled": True,
    "supports_vision": True,
    "context_window_tokens": 200000,
    "max_output_tokens": 64000,
    "input_cost_per_million_tokens_usd": 1,
    "output_cost_per_million_tokens_usd": 5.00,
}


def _preset_model(**overrides) -> LLMModel:
    model = LLMModel(
        name="Claude 4.5 Haiku",
        model_id="claude-haiku-4-5-20251001",
        is_preset=True,
        is_enabled=True,
    )
    for key, value in overrides.items():
        setattr(model, key, value)
    return model


def test_resolve_cost_prefers_override():
    assert LLMService._resolve_cost(3.33, 1) == 3.33
    assert LLMService._resolve_cost(0.0, 1) == 0.0
    assert LLMService._resolve_cost(None, 1) == 1
    assert LLMService._resolve_cost(None, None) is None


def test_catalog_sync_without_override_follows_catalog():
    model = _preset_model(
        input_cost_per_million_tokens_usd=999.0,
        output_cost_per_million_tokens_usd=999.0,
    )

    LLMService._apply_catalog_model_details(model, CATALOG_ENTRY)

    assert model.input_cost_per_million_tokens_usd == 1
    assert model.output_cost_per_million_tokens_usd == 5.00


def test_catalog_sync_preserves_admin_pricing_override():
    model = _preset_model(
        input_cost_per_million_tokens_usd=3.33,
        output_cost_per_million_tokens_usd=9.99,
        input_cost_per_million_tokens_usd_override=3.33,
        output_cost_per_million_tokens_usd_override=9.99,
    )

    LLMService._apply_catalog_model_details(model, CATALOG_ENTRY)

    assert model.input_cost_per_million_tokens_usd == 3.33
    assert model.output_cost_per_million_tokens_usd == 9.99


def test_catalog_sync_respects_partial_override():
    model = _preset_model(
        input_cost_per_million_tokens_usd=4.0,
        input_cost_per_million_tokens_usd_override=4.0,
    )

    LLMService._apply_catalog_model_details(model, CATALOG_ENTRY)

    assert model.input_cost_per_million_tokens_usd == 4.0
    assert model.output_cost_per_million_tokens_usd == 5.00

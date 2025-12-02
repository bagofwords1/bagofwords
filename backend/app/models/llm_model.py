from sqlalchemy import Column, String, JSON, ForeignKey, Boolean, Integer, Float
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema

LLM_MODEL_DETAILS = [
    {
        "name": "GPT-5.1",
        "model_id": "gpt-5.1",
        "provider_type": "openai",
        "is_preset": True,
        "is_enabled": True,
        "is_default": False,
        "context_window_tokens": 400000,
        "input_cost_per_million_tokens_usd": 1.25,
        "output_cost_per_million_tokens_usd": 10.00
    },
    {
        "name": "GPT-5",
        "model_id": "gpt-5",
        "provider_type": "openai",
        "is_preset": True,
        "is_enabled": True,
        "is_default": False,
        "context_window_tokens": 400000,
        "input_cost_per_million_tokens_usd": 1.25,
        "output_cost_per_million_tokens_usd": 10.00
    },

    {   "name": "GPT-4.1",
        "model_id": "gpt-4.1",
        "provider_type": "openai",
        "is_preset": True,
        "is_enabled": True,
        "is_default": True,
        "context_window_tokens": 1047576,
        "input_cost_per_million_tokens_usd": 2.00,
        "output_cost_per_million_tokens_usd": 8.00
    },
    {
        "name": "GPT-4.1 Mini",
        "model_id": "gpt-4.1-mini",
        "provider_type": "openai",
        "is_preset": True,
        "is_enabled": True,
        "is_default": False,
        "is_small_default": True,
        "context_window_tokens": 1047576,
        "input_cost_per_million_tokens_usd": 0.40,
        "output_cost_per_million_tokens_usd": 1.60
    },
    {
        "name": "Claude 4.5 Sonnet",
        "model_id": "claude-sonnet-4-5-20250929",
        "provider_type": "anthropic",
        "is_preset": True,
        "is_enabled": True,
        "is_default": True,
        "context_window_tokens": 200000,
        "input_cost_per_million_tokens_usd": 3.00,
        "output_cost_per_million_tokens_usd": 15.00

    },

        {
        "name": "Claude 4.5 Opus",
        "model_id": "claude-opus-4-5-20251101",
        "provider_type": "anthropic",
        "is_preset": True,
        "is_enabled": True,
        "is_default": True,
        "context_window_tokens": 200000,
        "input_cost_per_million_tokens_usd": 5.00,
        "output_cost_per_million_tokens_usd": 25.00

    },

    {
        "name": "Claude 4 Sonnet",
        "model_id": "claude-sonnet-4-20250514",
        "provider_type": "anthropic",
        "is_preset": True,
        "is_enabled": True,
        "is_default": True,
        "context_window_tokens": 1000000,
        "input_cost_per_million_tokens_usd": 3.00,
        "output_cost_per_million_tokens_usd": 15.00
    },
    {
        "name": "Claude 4 Opus",
        "model_id": "claude-opus-4-20250514",
        "provider_type": "anthropic",
        "is_preset": True,
        "is_enabled": True,
        "context_window_tokens": 1000000,
        "input_cost_per_million_tokens_usd": 15.00,
        "output_cost_per_million_tokens_usd": 75.00
    },
    {
        "name": "Claude 4.5 Haiku",
        "model_id": "claude-haiku-4-5-20251001",
        "provider_type": "anthropic",
        "is_preset": True,
        "is_enabled": True,
        "is_small_default": True,
        "is_default": False,
        "context_window_tokens": 200000,
        "input_cost_per_million_tokens_usd": 1,
        "output_cost_per_million_tokens_usd": 5.00
    },

    {
        "name": "Gemini 3 Pro Preview",
        "model_id": "gemini-3-pro-preview",
        "provider_type": "google",
        "is_preset": True,
        "is_enabled": True,
        "is_default": False,
        "is_small_default": False,
        "context_window_tokens": 200000,
        "input_cost_per_million_tokens_usd": 2.00,
        "output_cost_per_million_tokens_usd": 12.00
    },

    {
        "name": "Gemini 2.5 Pro",
        "model_id": "gemini-2.5-pro",
        "provider_type": "google",
        "is_preset": True,
        "is_enabled": True,
        "is_default": True,
        "context_window_tokens": 1047576,
        "input_cost_per_million_tokens_usd": 1.25,
        "output_cost_per_million_tokens_usd": 10.00
    },
    {
        "name": "Gemini 2.5 Flash",
        "model_id": "gemini-2.5-flash",
        "provider_type": "google",
        "is_preset": True,
        "is_enabled": True,
        "is_small_default": True,
        "context_window_tokens": 1047576,
        "input_cost_per_million_tokens_usd": 0.30,
        "output_cost_per_million_tokens_usd": 2.50
    }

]

class LLMModel(BaseSchema):
    __tablename__ = "llm_models"
    
    name = Column(String, nullable=False)
    model_id = Column(String, nullable=False)  # The actual model ID used with the provider
    is_custom = Column(Boolean, default=False)  # Whether this is a custom model ID
    config = Column(JSON, nullable=True)  # Model-specific configurations
    is_preset = Column(Boolean, default=False, nullable=False)  # If True, cannot be deleted
    is_enabled = Column(Boolean, default=True, nullable=False)  # Can be disabled but not deleted
    is_default = Column(Boolean, default=False, nullable=False)  # If True, this is the default model for the organization
    is_small_default = Column(Boolean, default=False, nullable=False)  # Optional small default model per organization
    # Token limits
    context_window_tokens = Column(Integer, nullable=True)  # Max prompt+completion tokens
    max_output_tokens = Column(Integer, nullable=True)  # Max model output tokens
    # Pricing (USD per million tokens)
    input_cost_per_million_tokens_usd = Column(Float, nullable=True)
    output_cost_per_million_tokens_usd = Column(Float, nullable=True)
    
    provider_id = Column(String, ForeignKey('llm_providers.id'), nullable=False)
    provider = relationship("LLMProvider", back_populates="models", lazy="selectin")
    organization_id = Column(String, ForeignKey('organizations.id'), nullable=False)
    organization = relationship("Organization", back_populates="llm_models", lazy="selectin")

    # Pricing helpers -----------------------------------------------------
    def _get_static_details(self) -> dict | None:
        for detail in LLM_MODEL_DETAILS:
            if detail.get("model_id") == self.model_id:
                return detail
        return None

    def get_input_cost_rate(self) -> float | None:
        if self.input_cost_per_million_tokens_usd is not None:
            return float(self.input_cost_per_million_tokens_usd)
        detail = self._get_static_details()
        if detail:
            return detail.get("input_cost_per_million_tokens_usd")
        return None

    def get_output_cost_rate(self) -> float | None:
        if self.output_cost_per_million_tokens_usd is not None:
            return float(self.output_cost_per_million_tokens_usd)
        detail = self._get_static_details()
        if detail:
            return detail.get("output_cost_per_million_tokens_usd")
        return None

from sqlalchemy import Column, String, JSON, ForeignKey, Boolean, Integer, Float
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema

LLM_MODEL_DETAILS = [
    {
        "name": "GPT-5.6 Sol",
        "model_id": "gpt-5.6-sol",
        "provider_type": "openai",
        "is_preset": True,
        "is_enabled": True,
        "is_default": False,
        "supports_vision": True,
        "context_window_tokens": 1050000,
        "max_output_tokens": 128000,
        "input_cost_per_million_tokens_usd": 5.00,
        "output_cost_per_million_tokens_usd": 30.00
    },
    {
        "name": "GPT-5.6 Terra",
        "model_id": "gpt-5.6-terra",
        "provider_type": "openai",
        "is_preset": True,
        "is_enabled": True,
        "is_default": True,
        "supports_vision": True,
        "context_window_tokens": 1050000,
        "max_output_tokens": 128000,
        "input_cost_per_million_tokens_usd": 2.50,
        "output_cost_per_million_tokens_usd": 15.00
    },
    {
        "name": "GPT-5.6 Luna",
        "model_id": "gpt-5.6-luna",
        "provider_type": "openai",
        "is_preset": True,
        "is_enabled": True,
        "is_default": False,
        "is_small_default": True,
        "supports_vision": True,
        "context_window_tokens": 1050000,
        "max_output_tokens": 128000,
        "input_cost_per_million_tokens_usd": 1.00,
        "output_cost_per_million_tokens_usd": 6.00
    },
    {
        "name": "GPT-5.5",
        "model_id": "gpt-5.5",
        "provider_type": "openai",
        "is_preset": True,
        "is_enabled": True,
        "is_default": False,
        "supports_vision": True,
        "context_window_tokens": 1050000,
        "max_output_tokens": 128000,
        "input_cost_per_million_tokens_usd": 5.00,
        "output_cost_per_million_tokens_usd": 30.00
    },
    {
        "name": "GPT-5.4",
        "model_id": "gpt-5.4",
        "provider_type": "openai",
        "is_preset": True,
        "is_enabled": False,
        "is_default": False,
        "supports_vision": True,
        "context_window_tokens": 400000,
        "input_cost_per_million_tokens_usd": 2.50,
        "output_cost_per_million_tokens_usd": 15.00
    },
    {
        "name": "GPT-5.4 Mini",
        "model_id": "gpt-5.4-mini",
        "provider_type": "openai",
        "is_preset": True,
        "is_enabled": False,
        "is_default": False,
        "is_small_default": False,
        "supports_vision": True,
        "context_window_tokens": 400000,
        "input_cost_per_million_tokens_usd": 0.75,
        "output_cost_per_million_tokens_usd": 4.50
    },
    {
        "name": "GPT-5.2",
        "model_id": "gpt-5.2",
        "provider_type": "openai",
        "is_preset": True,
        "is_enabled": False,
        "is_default": False,
        "supports_vision": True,
        "context_window_tokens": 400000,
        "input_cost_per_million_tokens_usd": 1.75,
        "output_cost_per_million_tokens_usd": 14.00
    },
    {
        "name": "Claude Fable 5",
        "model_id": "claude-fable-5",
        "provider_type": "anthropic",
        "is_preset": True,
        "is_enabled": True,
        "is_default": False,
        "supports_vision": True,
        "context_window_tokens": 1000000,
        "input_cost_per_million_tokens_usd": 10.00,
        "output_cost_per_million_tokens_usd": 50.00
    },
    {
        "name": "Claude Sonnet 5",
        "model_id": "claude-sonnet-5",
        "provider_type": "anthropic",
        "is_preset": True,
        "is_enabled": True,
        "is_default": True,
        "supports_vision": True,
        "context_window_tokens": 1000000,
        "input_cost_per_million_tokens_usd": 3.00,
        "output_cost_per_million_tokens_usd": 15.00
    },
    {
        "name": "Claude Opus 4.8",
        "model_id": "claude-opus-4-8",
        "provider_type": "anthropic",
        "is_preset": True,
        "is_enabled": True,
        "is_default": False,
        "supports_vision": True,
        "context_window_tokens": 1000000,
        "input_cost_per_million_tokens_usd": 5.00,
        "output_cost_per_million_tokens_usd": 25.00
    },
    {
        "name": "Claude 4.6 Sonnet",
        "model_id": "claude-sonnet-4-6",
        "provider_type": "anthropic",
        "is_preset": True,
        "is_enabled": True,
        "is_default": False,
        "supports_vision": True,
        "context_window_tokens": 1000000,
        "input_cost_per_million_tokens_usd": 3.00,
        "output_cost_per_million_tokens_usd": 15.00
    },
    {
        "name": "Claude 4.5 Haiku",
        "model_id": "claude-haiku-4-5-20251001",
        "provider_type": "anthropic",
        "is_preset": True,
        "is_enabled": True,
        "is_small_default": True,
        "is_default": False,
        "supports_vision": True,
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
        "supports_vision": True,
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
        "supports_vision": True,
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
        "supports_vision": True,
        "context_window_tokens": 1047576,
        "input_cost_per_million_tokens_usd": 0.30,
        "output_cost_per_million_tokens_usd": 2.50
    },
    {
        # Image-generation model (produces images), not a chat model. Gated by
        # supports_image_generation; consumed by LLM.generate_image / the
        # generate_image tool. Pricing is per token: text input $5/M, image
        # output tokens $40/M (OpenAI Images API, gpt-image-1).
        "name": "GPT Image 1",
        "model_id": "gpt-image-1",
        "provider_type": "openai",
        "is_preset": True,
        "is_enabled": True,
        "is_default": False,
        "is_small_default": False,
        "supports_vision": False,
        "supports_image_generation": True,
        "input_cost_per_million_tokens_usd": 5.00,
        "output_cost_per_million_tokens_usd": 40.00
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
    is_restricted = Column(Boolean, default=False, nullable=False)  # If True, usable only by granted principals (plus admins/org defaults). EE feature.
    supports_vision = Column(Boolean, default=False, nullable=False)  # Effective flag: whether model accepts image inputs
    # Manual admin override for vision. NULL = follow the catalog (LLM_MODEL_DETAILS); True/False = admin-set,
    # persisted across catalog re-syncs. `supports_vision` above is the resolved value inference reads.
    supports_vision_override = Column(Boolean, nullable=True)
    # Whether the model *produces* images (image-generation models like gpt-image-1),
    # as opposed to supports_vision which is about accepting image *inputs*. Resolved
    # from the catalog on sync; gates LLM.generate_image and the generate_image tool.
    supports_image_generation = Column(Boolean, default=False, nullable=False)
    # Manual admin override for image generation. NULL = follow the catalog; True/False
    # = admin-set (e.g. marking a custom model as an image model), persisted across
    # catalog re-syncs. `supports_image_generation` above is the resolved value.
    supports_image_generation_override = Column(Boolean, nullable=True)
    # Token limits
    context_window_tokens = Column(Integer, nullable=True)  # Max prompt+completion tokens
    # Manual admin override for the context window. NULL = follow the catalog (LLM_MODEL_DETAILS);
    # a value = admin-set (e.g. a Bedrock deployment capped at 100k), persisted across catalog
    # re-syncs. `context_window_tokens` above is the resolved value the agent's token budget reads.
    context_window_tokens_override = Column(Integer, nullable=True)
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

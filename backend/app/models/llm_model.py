from sqlalchemy import Column, String, JSON, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema

LLM_MODEL_DETAILS = [
    {
        "name": "GPT-5",
        "model_id": "gpt-5",
        "provider_type": "openai",
        "is_preset": True,
        "is_enabled": True,
        "is_default": False
    },

    {   "name": "GPT-4.1",
        "model_id": "gpt-4.1",
        "provider_type": "openai",
        "is_preset": True,
        "is_enabled": True,
        "is_default": True
    },
    {
        "name": "GPT-4.1 Mini",
        "model_id": "gpt-4.1-mini",
        "provider_type": "openai",
        "is_preset": True,
        "is_enabled": True,
        "is_default": False
    },
    {
        "name": "GPT-4o",
        "model_id": "gpt-4o",
        "provider_type": "openai",
        "is_preset": True,
        "is_enabled": True,
        "is_default": False
    },
    {
        "name": "GPT-4o Mini",
        "model_id": "gpt-4o-mini",
        "provider_type": "openai",
        "is_preset": True,
        "is_enabled": True
    },
    {
        "name": "o1",
        "model_id": "o1",
        "provider_type": "openai",
        "is_preset": True,
        "is_enabled": True
    },
    {
        "name": "o1 Mini",
        "model_id": "o1-mini",
        "provider_type": "openai",
        "is_preset": True,
        "is_enabled": True
    },
    {
        "name": "Claude 4 Sonnet",
        "model_id": "claude-sonnet-4-20250514",
        "provider_type": "anthropic",
        "is_preset": True,
        "is_enabled": True,
        "is_default": True
    },
    {
        "name": "Claude 4 Opus",
        "model_id": "claude-opus-4-20250514",
        "provider_type": "anthropic",
        "is_preset": True,
        "is_enabled": True
    },
    {
        "name": "Claude 3.7 Sonnet",
        "model_id": "claude-3-7-sonnet-20250219",
        "provider_type": "anthropic",
        "is_preset": True,
        "is_enabled": True
    },
    {
        "name": "Claude 3.5 Sonnet",
        "model_id": "claude-3-sonnet-20240229",
        "provider_type": "anthropic",
        "is_preset": False,
        "is_enabled": False,
    },
    {
        "name": "Claude 3 Haiku",
        "model_id": "claude-3-haiku-20240307",
        "provider_type": "anthropic",
        "is_preset": False,
        "is_enabled": False
    },
    {
        "name": "Claude 3 Opus",
        "model_id": "claude-3-opus-20240229",
        "provider_type": "anthropic",
        "is_preset": False,
        "is_enabled": False
    },
    {
        "name": "Gemini Pro",
        "model_id": "gemini-1.5-pro",
        "provider_type": "google",
        "is_preset": False,
        "is_enabled": False
    },
    {
        "name": "Gemini 1.5 Flash",
        "model_id": "gemini-1.5-flash",
        "provider_type": "google",
        "is_preset": False,
        "is_enabled": False
    },
    {
        "name": "BOW Small",
        "model_id": "gpt-4o-mini",
        "provider_type": "bow",
        "is_preset": False,
        "is_enabled": False
    },
    {
        "name": "o1 (Azure)",
        "model_id": "o1",
        "provider_type": "azure",
        "is_preset": True,
        "is_enabled": True
    },
    {
        "name": "o1-mini (Azure)",
        "model_id": "o1-mini",
        "provider_type": "azure",
        "is_preset": True,
        "is_enabled": True
    },
    {
        "name": "o3 (Azure)",
        "model_id": "o3",
        "provider_type": "azure",
        "is_preset": True,
        "is_enabled": True
    },
    {
        "name": "o3-mini (Azure)",
        "model_id": "o3-mini",
        "provider_type": "azure",
        "is_preset": True,
        "is_enabled": True
    },
    {
        "name": "o4-mini (Azure)",
        "model_id": "o4-mini",
        "provider_type": "azure",
        "is_preset": True,
        "is_enabled": True
    },
    {
        "name": "GPT-4.1 (Azure)",
        "model_id": "gpt-4.1",
        "provider_type": "azure",
        "is_preset": True,
        "is_enabled": True
    },
    {
        "name": "GPT-4o (Azure)",
        "model_id": "gpt-4o",
        "provider_type": "azure",
        "is_preset": True,
        "is_enabled": True
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
    
    provider_id = Column(String, ForeignKey('llm_providers.id'), nullable=False)
    provider = relationship("LLMProvider", back_populates="models", lazy="selectin")
    organization_id = Column(String, ForeignKey('organizations.id'), nullable=False)
    organization = relationship("Organization", back_populates="llm_models", lazy="selectin")

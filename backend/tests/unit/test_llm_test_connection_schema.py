import pytest
from pydantic import ValidationError

from app.schemas.llm_schema import LLMProviderCreate, LLMProviderTestConnection


def test_new_provider_requires_api_key():
    """Without a provider_id (new provider), missing required secrets fail."""
    with pytest.raises(ValidationError):
        LLMProviderTestConnection(
            name="openai temp",
            provider_type="openai",
            credentials={},  # missing api_key
        )


def test_existing_provider_allows_blank_credentials():
    """With a provider_id (existing provider), blank credentials are allowed
    because they fall back to the stored values."""
    payload = LLMProviderTestConnection(
        provider_id="prov-123",
        name="openai",
        provider_type="openai",
        credentials={"api_key": None},
    )
    assert payload.provider_id == "prov-123"
    # Raw credentials passed through untouched for existing providers
    assert payload.credentials == {"api_key": None}


def test_existing_provider_overrides_with_new_key():
    """A supplied key on an existing provider is preserved in the payload."""
    payload = LLMProviderTestConnection(
        provider_id="prov-123",
        name="openai",
        provider_type="openai",
        credentials={"api_key": "sk-new"},
    )
    assert payload.credentials["api_key"] == "sk-new"


def test_unknown_provider_type_rejected():
    with pytest.raises(ValidationError):
        LLMProviderTestConnection(
            provider_id="prov-123",
            name="weird",
            provider_type="not_a_provider",
            credentials={},
        )


def test_create_schema_still_strict():
    """The create schema is unchanged: api_key remains required."""
    with pytest.raises(ValidationError):
        LLMProviderCreate(
            name="openai",
            provider_type="openai",
            credentials={},
        )

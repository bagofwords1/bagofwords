
from __future__ import annotations
from pydantic import BaseModel, Field, validator
from typing import Optional, Dict, Any
import json

# Base Provider Classes
class LLMProviderBase(BaseModel):
    name: str
    provider_type: str  # e.g., "anthropic", "openai", "google"
    config: Optional[Dict[str, Any]] = None

class LLMProviderSchema(LLMProviderBase):
    id: str
    organization_id: str
    is_preset: bool
    is_enabled: bool
    credentials: Optional[dict] = None
    additional_config: Optional[Dict[str, Any]] = None
    models: list[LLMModelSchema] = []

    @validator('config', pre=True)
    def parse_config(cls, value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                raise ValueError('Invalid JSON string for config')
        return value

    class Config:
        from_attributes = True

class LLMProviderCreate(LLMProviderBase):
    credentials: dict  # Will be validated based on the provider type
    models: list[dict] = []

    @validator('credentials')
    def validate_credentials(cls, v, values):
        if 'provider_type' not in values:
            raise ValueError('Provider type must be specified')
        
        credential_schemas = {
            'anthropic': AnthropicCredentials,
            'openai': OpenAICredentials,
            'google': GoogleCredentials,
            'azure': AzureCredentials,
            'custom': CustomCredentials,
            'bedrock': BedrockCredentials,
        }
        
        schema = credential_schemas.get(values['provider_type'])
        if not schema:
            raise ValueError(f'Unknown provider type: {values["provider_type"]}')
        
        return schema(**v).dict()

class LLMProviderTestConnection(LLMProviderBase):
    # When set, the test targets an already-saved provider. Blank credential
    # fields then fall back to the stored (encrypted) values.
    provider_id: Optional[str] = None
    credentials: dict = {}
    models: list[dict] = []

    @validator('credentials')
    def validate_credentials(cls, v, values):
        if 'provider_type' not in values:
            raise ValueError('Provider type must be specified')

        credential_schemas = {
            'anthropic': AnthropicCredentials,
            'openai': OpenAICredentials,
            'google': GoogleCredentials,
            'azure': AzureCredentials,
            'custom': CustomCredentials,
            'bedrock': BedrockCredentials,
        }

        schema = credential_schemas.get(values['provider_type'])
        if not schema:
            raise ValueError(f'Unknown provider type: {values["provider_type"]}')

        # For an existing provider, the payload may omit/blank required secrets
        # (they fall back to stored values), so skip strict schema validation
        # and pass the raw partial credentials through.
        if values.get('provider_id'):
            return v

        return schema(**v).dict()

class LLMProviderUpdate(BaseModel):
    name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    credentials: Optional[dict] = None
    additional_config: Optional[Dict[str, Any]] = None
    models: list[LLMModelSchema] = []

# Provider-specific Credentials
class AnthropicCredentials(BaseModel):
    api_key: str

class AnthropicConfig(BaseModel):
    max_tokens: Optional[int] = 4096
    temperature: Optional[float] = 0.7

class OpenAICredentials(BaseModel):
    api_key: str
    base_url: Optional[str] = None
    # Per-provider opt-in for native web search (Responses API tool). Persisted
    # to additional_config, not encrypted. Only meaningful without a custom
    # base_url (custom base_url → Chat Completions, which has no web_search).
    enable_web_search: Optional[bool] = None

class BowCredentials(BaseModel):
    api_key: str

class OpenAIConfig(BaseModel):
    max_tokens: Optional[int] = 2048
    temperature: Optional[float] = 0.7

class GoogleCredentials(BaseModel):
    api_key: str

class GoogleConfig(BaseModel):
    max_output_tokens: Optional[int] = 2048
    temperature: Optional[float] = 0.3
    top_p: Optional[float] = 0.8
    top_k: Optional[int] = 40

# Azure endpoint surfaces. 'auto' infers from the hostname (the default and
# right answer for public Azure); the explicit modes override that inference
# for private DNS / Private Link endpoints.
AZURE_ENDPOINT_MODE_AUTO = "auto"
AZURE_ENDPOINT_MODE_AZURE_OPENAI = "azure_openai"
AZURE_ENDPOINT_MODE_FOUNDRY = "foundry"
AZURE_ENDPOINT_MODES = {
    AZURE_ENDPOINT_MODE_AUTO,
    AZURE_ENDPOINT_MODE_AZURE_OPENAI,
    AZURE_ENDPOINT_MODE_FOUNDRY,
}

class AzureCredentials(BaseModel):
    api_key: str
    endpoint_url: str
    # Which Azure surface ``endpoint_url`` points at. Azure OpenAI resources
    # (``<res>.openai.azure.com``) serve the deployment-scoped route the Azure
    # SDK builds. Azure AI Foundry resources (``<res>.services.ai.azure.com``)
    # do not serve that route at all; they answer on ``/openai/v1`` for
    # OpenAI-family deployments and ``/anthropic/v1`` for Anthropic ones. Each
    # needs a different client, so the surface has to be known before the first
    # request. Default 'auto' infers it from the hostname — the explicit values
    # exist for private DNS / Private Link, where the hostname is a customer
    # domain that gives nothing away.
    # Persisted to additional_config, not encrypted.
    endpoint_mode: Optional[str] = None  # 'auto' | 'azure_openai' | 'foundry'
    # Azure OpenAI api-version pin. Only used on the azure_openai surface (the
    # v1 surfaces take no api-version). None keeps the client's default.
    api_version: Optional[str] = None
    # Opt-in to Azure OpenAI's Responses API (/openai/v1) instead of Chat
    # Completions. Off by default — only some Azure regions serve Responses.
    # Persisted to additional_config, not encrypted.
    use_responses_api: Optional[bool] = None
    # Per-provider opt-in for native web search. Only effective when
    # use_responses_api is on (web search is a Responses-API tool).
    enable_web_search: Optional[bool] = None

    @validator('endpoint_mode')
    def validate_endpoint_mode(cls, v):
        if v in (None, ''):
            return None
        if v not in AZURE_ENDPOINT_MODES:
            raise ValueError(
                f"endpoint_mode must be one of {sorted(AZURE_ENDPOINT_MODES)}"
            )
        return v

class AzureConfig(BaseModel):
    max_tokens: Optional[int] = 2048
    temperature: Optional[float] = 0.7

class CustomCredentials(BaseModel):
    """Credentials for OpenAI-compatible APIs (Ollama, Groq, Together AI, LM Studio, vLLM, etc.)"""
    base_url: str  # Required - the OpenAI-compatible endpoint
    api_key: Optional[str] = None  # Optional - some local servers don't require auth
    verify_ssl: Optional[bool] = True  # Optional - set to False to disable SSL certificate verification

class CustomConfig(BaseModel):
    max_tokens: Optional[int] = 4096
    temperature: Optional[float] = 0.7

class BedrockCredentials(BaseModel):
    """Credentials for AWS Bedrock. Supports API key auth, access key auth, or IAM auth (from environment)."""
    region: str = Field(..., description="AWS region (e.g. us-east-1, eu-west-1)")
    auth_mode: str = Field("iam", description="Authentication mode: 'api_key', 'access_keys', or 'iam'")
    api_key: Optional[str] = Field(None, description="Bedrock API key (only for api_key auth mode)")
    aws_access_key_id: Optional[str] = Field(None, description="AWS access key ID (only for access_keys auth mode)")
    aws_secret_access_key: Optional[str] = Field(None, description="AWS secret access key (only for access_keys auth mode)")

class BedrockConfig(BaseModel):
    max_tokens: Optional[int] = 4096
    temperature: Optional[float] = 0.7

# Model Classes
class LLMModelBase(BaseModel):
    name: str = None
    model_id: str
    is_default: bool = False
    is_small_default: bool = False
    supports_vision: bool = False
    # Manual admin override for vision. None = follow the catalog; True/False = explicit, survives catalog re-syncs.
    supports_vision_override: Optional[bool] = None
    # Whether the model produces images (gpt-image-1). Such models are not chat
    # models and are excluded from the chat/agent model picker.
    supports_image_generation: bool = False
    # Manual admin override for image generation. None = follow the catalog;
    # True/False = explicit (e.g. mark a custom model as an image model), survives
    # catalog re-syncs.
    supports_image_generation_override: Optional[bool] = None
    context_window_tokens: Optional[int] = None
    # Manual admin override for the context window. None = follow the catalog; a value is explicit and survives catalog re-syncs.
    context_window_tokens_override: Optional[int] = None
    max_output_tokens: Optional[int] = None
    input_cost_per_million_tokens_usd: Optional[float] = None
    output_cost_per_million_tokens_usd: Optional[float] = None
    config: Optional[Dict[str, Any]] = None

class LLMModelSchema(LLMModelBase):
    id: Optional[str] = None  # Optional for new models
    provider_id: Optional[str] = None  # Optional for new models
    is_preset: bool = False
    is_enabled: bool = True
    is_custom: bool = False
    is_restricted: bool = False
    # Whether this model is the CALLER's personal default (memberships.default_llm_model_id).
    # Computed per-request in LLMService.get_models — not a column.
    is_user_default: bool = False

    class Config:
        from_attributes = True

class LLMModelSchemaWithProvider(LLMModelSchema):
    provider: LLMProviderSchema

class LLMModelCreate(LLMModelBase):
    provider_id: str
    is_custom: bool = False

class LLMModelCreateInProvider(LLMModelBase):
    is_custom: bool = False

class LLMModelUpdate(BaseModel):
    name: Optional[str] = None
    # Only editable for custom models on providers whose model ids are
    # deployment/ARN identifiers the admin owns (azure, custom, bedrock).
    model_id: Optional[str] = None
    config: Optional[Dict[str, Any]] = None

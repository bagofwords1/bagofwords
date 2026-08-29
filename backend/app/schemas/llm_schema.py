
from __future__ import annotations
from pydantic import BaseModel, Field, validator
from typing import Optional, Dict, Any, List
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

class HeaderInjectionRule(BaseModel):
    """Forward the signed-in user's identity as an HTTP header on LLM requests.

    ``source`` uses the same whitelisted grammar as MCP context forwarding:
    user.email / user.name / user.id / membership.role /
    membership.attr:<key> / static:<text> (with {atom} interpolation).
    """
    header: str
    source: str


class ProviderHeadersMixin(BaseModel):
    """Custom outbound headers, shared by every provider's credentials.

    Both fields persist to additional_config (not encrypted) — mirroring how
    MCP connections store their ``headers`` / ``header_injection`` config.
    ``headers`` are static key/values sent on every request; ``header_injection``
    rules resolve per-user so gateways/proxies can attribute cost per caller.
    """
    headers: Optional[Dict[str, str]] = None
    header_injection: Optional[List[HeaderInjectionRule]] = None


class AnthropicCredentials(ProviderHeadersMixin):
    api_key: str

class AnthropicConfig(BaseModel):
    max_tokens: Optional[int] = 4096
    temperature: Optional[float] = 0.7

class OpenAICredentials(ProviderHeadersMixin):
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

class GoogleCredentials(ProviderHeadersMixin):
    api_key: str

class GoogleConfig(BaseModel):
    max_output_tokens: Optional[int] = 2048
    temperature: Optional[float] = 0.3
    top_p: Optional[float] = 0.8
    top_k: Optional[int] = 40

class AzureCredentials(ProviderHeadersMixin):
    # Azure OpenAI resource root or Azure AI Foundry resource root. Which client
    # serves it is derived, not configured: an Anthropic deployment goes to the
    # Messages API on any Azure hostname, and the OpenAI-shaped routes are
    # chosen from the hostname (see app.ai.llm.llm).
    endpoint_url: str
    # Authentication mode. 'api_key' (default) uses a static Azure OpenAI key.
    # 'entra_client_secret' authenticates as a Microsoft Entra ID service
    # principal (tenant_id + client_id + client_secret); 'entra_default' uses
    # azure-identity's DefaultAzureCredential (managed identity, workload
    # identity, Azure CLI, env vars). Entra modes acquire AAD tokens for the
    # Cognitive Services scope and refresh them automatically.
    auth_mode: str = Field("api_key", description="Authentication mode: 'api_key', 'entra_client_secret', or 'entra_default'")
    api_key: Optional[str] = Field(None, description="Azure OpenAI API key (only for api_key auth mode)")
    tenant_id: Optional[str] = Field(None, description="Entra ID tenant ID (only for entra_client_secret auth mode)")
    client_id: Optional[str] = Field(None, description="Entra ID application (client) ID (only for entra_client_secret auth mode)")
    client_secret: Optional[str] = Field(None, description="Entra ID client secret (only for entra_client_secret auth mode)")
    # Opt-in to Azure OpenAI's Responses API (/openai/v1) instead of Chat
    # Completions. Off by default — only some Azure regions serve Responses.
    # Persisted to additional_config, not encrypted. Ignored under Entra auth
    # modes (the Responses path has no AAD token-provider hook yet).
    use_responses_api: Optional[bool] = None
    # Per-provider opt-in for native web search. Only effective when
    # use_responses_api is on (web search is a Responses-API tool).
    enable_web_search: Optional[bool] = None

    @validator('auth_mode')
    def validate_auth_mode(cls, v):
        allowed = {'api_key', 'entra_client_secret', 'entra_default'}
        if v not in allowed:
            raise ValueError(f"auth_mode must be one of: {', '.join(sorted(allowed))}")
        return v

    @validator('client_secret', always=True)
    def validate_mode_fields(cls, v, values):
        mode = values.get('auth_mode', 'api_key')
        if mode == 'api_key' and not values.get('api_key'):
            raise ValueError('api_key is required for api_key auth mode')
        if mode == 'entra_client_secret':
            missing = [f for f in ('tenant_id', 'client_id') if not values.get(f)]
            if not v:
                missing.append('client_secret')
            if missing:
                raise ValueError(f"entra_client_secret auth mode requires: {', '.join(missing)}")
        return v

class AzureConfig(BaseModel):
    max_tokens: Optional[int] = 2048
    temperature: Optional[float] = 0.7

class CustomCredentials(ProviderHeadersMixin):
    """Credentials for OpenAI-compatible APIs (Ollama, Groq, Together AI, LM Studio, vLLM, etc.)"""
    base_url: str  # Required - the OpenAI-compatible endpoint
    api_key: Optional[str] = None  # Optional - some local servers don't require auth
    verify_ssl: Optional[bool] = True  # Optional - set to False to disable SSL certificate verification

class CustomConfig(BaseModel):
    max_tokens: Optional[int] = 4096
    temperature: Optional[float] = 0.7

class BedrockCredentials(ProviderHeadersMixin):
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


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
        }
        
        schema = credential_schemas.get(values['provider_type'])
        if not schema:
            raise ValueError(f'Unknown provider type: {values["provider_type"]}')
        
        return schema(**v).dict()

class LLMProviderUpdate(BaseModel):
    name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    credentials: Optional[dict] = None
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

class BowCredentials(BaseModel):
    api_key: str

class OpenAIConfig(BaseModel):
    max_tokens: Optional[int] = 2048
    temperature: Optional[float] = 0.7

class GoogleCredentials(BaseModel):
    api_key: str

class GoogleConfig(BaseModel):
    max_output_tokens: Optional[int] = 2048
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 0.8
    top_k: Optional[int] = 40

class AzureCredentials(BaseModel):
    api_key: str
    endpoint_url: str

class AzureConfig(BaseModel):
    max_tokens: Optional[int] = 2048
    temperature: Optional[float] = 0.7

# Model Classes
class LLMModelBase(BaseModel):
    name: str = None
    model_id: str
    is_default: bool = False
    config: Optional[Dict[str, Any]] = None

class LLMModelSchema(LLMModelBase):
    id: Optional[str] = None  # Optional for new models
    provider_id: Optional[str] = None  # Optional for new models
    is_preset: bool = False
    is_enabled: bool = True
    is_custom: bool = False

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
    config: Optional[Dict[str, Any]] = None

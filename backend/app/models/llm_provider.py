from sqlalchemy import Column, String, JSON, ForeignKey, Boolean, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
import json
from cryptography.fernet import Fernet
from app.settings.config import settings
from app.schemas.llm_schema import AnthropicCredentials, OpenAICredentials, GoogleCredentials, BowCredentials, AzureCredentials

# LLM Provider Classes

LLM_PROVIDER_DETAILS = [
    {
        "type": "openai",
        "name": "OpenAI",
        "description": "OpenAI's API for accessing their LLM models",
        "config": "OpenAIConfig",
        "credentials": OpenAICredentials.schema()
    },
    {
        "type": "anthropic",
        "name": "Anthropic",
        "description": "Anthropic's API for accessing their LLM models",
        "config": "AnthropicConfig",
        "credentials": AnthropicCredentials.schema()
    },
    {
        "type": "azure",
        "name": "Azure OpenAI",
        "description": "Azure OpenAI Service for accessing OpenAI models",
        "config": "AzureConfig",
        "credentials": AzureCredentials.schema()
    },
     {
         "type": "google",
         "name": "Google",
         "description": "Google's API for accessing their LLM models",
         "config": "GoogleConfig",
         "credentials": GoogleCredentials.schema()
     }
]

BOW_PROVIDER_DETAILS = {
    "type": "bow",
    "name": "BOW",
    "description": "BOW's API for accessing their LLM models",
    "config": "BowConfig",
    "credentials": BowCredentials.schema()
}



class LLMProvider(BaseSchema):
    __tablename__ = "llm_providers"
    __table_args__ = (
        UniqueConstraint('organization_id', 'name', name='uq_llm_providers_org_name'),
    )
    
    name = Column(String, nullable=False)
    provider_type = Column(String, nullable=False)  # 'openai', 'anthropic', etc.
    api_key = Column(Text, nullable=True)  # Nullable because it might use default from config
    api_secret = Column(Text, nullable=True)
    additional_config = Column(JSON, nullable=True)  # For provider-specific settings
    
    organization_id = Column(String, ForeignKey('organizations.id'), nullable=False)
    organization = relationship("Organization", back_populates="llm_providers")
    
    is_preset = Column(Boolean, default=False, nullable=False)  # If True, cannot be deleted
    is_enabled = Column(Boolean, default=True, nullable=False)  # Can be disabled but not deleted
    use_preset_credentials = Column(Boolean, default=True, nullable=False)
    
    models = relationship("LLMModel", back_populates="provider", lazy="joined")


    def encrypt_credentials(self, api_key: str, api_secret: str):
        fernet = Fernet(settings.bow_config.encryption_key)
        self.api_key = fernet.encrypt(json.dumps(api_key).encode()).decode()
        self.api_secret = fernet.encrypt(json.dumps(api_secret).encode()).decode()

    def decrypt_credentials(self) -> dict:
        fernet = Fernet(settings.bow_config.encryption_key)
        return json.loads(fernet.decrypt(self.api_key.encode()).decode()), json.loads(fernet.decrypt(self.api_secret.encode()).decode())
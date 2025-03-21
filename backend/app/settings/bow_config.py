from typing import List, Optional
from pydantic import BaseModel, Field, validator
import os
import secrets
import base64


class LLMModel(BaseModel):
    model_id: str
    model_name: str
    is_default: bool = False
    is_enabled: bool = True


class LLMProvider(BaseModel):
    provider_type: str
    provider_name: str
    api_key: str
    models: List[LLMModel]

class Intercom(BaseModel):
    enabled: bool = False


class DeploymentConfig(BaseModel):
    type: str = "self_hosted"


class FeatureFlags(BaseModel):
    allow_uninvited_signups: bool = False
    allow_multiple_organizations: bool = False
    verify_emails: bool = False


class GoogleOAuth(BaseModel):
    enabled: bool = False
    client_id: Optional[str] = None
    client_secret: Optional[str] = None


class SMTPSettings(BaseModel):
    host: str = "smtp.resend.com"
    port: int = 587
    username: str = "resend"
    password: str

class Stripe(BaseModel):
    api_key: str = None
    webhook_secret: str = None

class Database(BaseModel):
    url: str = Field(
        default_factory=lambda: os.getenv(
            "BOW_DATABASE_URL", 
            "sqlite:////app/backend/db/app.db"
        )
    )

def generate_fernet_key():
    # Generate a valid Fernet-compatible key (32 url-safe base64-encoded bytes)
    key = secrets.token_bytes(32)
    return base64.urlsafe_b64encode(key).decode()

class BowConfig(BaseModel):
    deployment: DeploymentConfig = DeploymentConfig()
    base_url: Optional[str] = Field(default="http://0.0.0.0:3000")
    features: FeatureFlags = FeatureFlags()
    google_oauth: GoogleOAuth = GoogleOAuth()
    default_llm: List[LLMProvider] = []
    smtp_settings: SMTPSettings = None
    encryption_key: str = Field(
        default_factory=generate_fernet_key,
        description="Encryption key for sensitive data",
        env="BOW_ENCRYPTION_KEY"
    )
    stripe: Stripe = Stripe()
    database: Database = Database()

    @validator('encryption_key')
    def validate_encryption_key(cls, v):
        # If the value is empty or still the placeholder, generate a valid key:
        if not v or v.strip() in {"", "${BOW_ENCRYPTION_KEY}"}:
            return generate_fernet_key()
        return v
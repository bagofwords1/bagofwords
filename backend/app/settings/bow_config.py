from typing import List, Optional
from pydantic import BaseModel, Field, validator, ConfigDict, AliasGenerator
from pydantic.alias_generators import to_camel
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

class Telemetry(BaseModel):
    enabled: bool = True

class Swagger(BaseModel):
    enabled: bool = False


class DeploymentConfig(BaseModel):
    type: str = "self_hosted"

class FeatureFlags(BaseModel):
    allow_uninvited_signups: bool = False
    allow_multiple_organizations: bool = False
    verify_emails: bool = False

class OTELConfig(BaseModel):
    model_config = ConfigDict(
        alias_generator=AliasGenerator(
            validation_alias=to_camel,
            serialization_alias=to_camel,
        )
    )
    enabled: bool = False
    service_name: str = "bagofwords-backend"
    traces_endpoint: str = "http://localhost:4317"
    protocol: str = "grpc"  # grpc or http/protobuf
    headers: Optional[str] = ""  # format: key1=value1,key2=value2

    def get_headers(self) -> dict:
        """Parse OTLP headers from environment variable format: key1=value1,key2=value2"""
        if not self.headers:
            return {}
        headers = {}
        for pair in self.headers.split(","):
            if "=" in pair:
                key, value = pair.split("=", 1)
                headers[key.strip()] = value.strip()
        return headers

class AuthConfig(BaseModel):
    # local_only | sso_only | hybrid
    mode: str = "hybrid"


class GoogleOAuth(BaseModel):
    enabled: bool = False
    client_id: Optional[str] = None
    client_secret: Optional[str] = None


class OIDCProvider(BaseModel):
    name: str
    enabled: bool = False
    issuer: str
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    scopes: List[str] = ["openid", "profile", "email"]
    # UI niceties
    label: Optional[str] = None
    icon: Optional[str] = None
    # Advanced options
    pkce: bool = True
    client_auth_method: str = "basic"  # basic | post
    discovery: bool = True
    uid_claim: Optional[str] = "sub"
    redirect_path: Optional[str] = None
    extra_authorize_params: dict = {}
    extra_token_params: dict = {}


class SMTPSettings(BaseModel):
    host: str = "smtp.resend.com"
    port: int = 587
    username: str = "resend"
    password: Optional[str] = None
    from_name: str = "Bag of words"
    from_email: str = "hi@bagofwords.com"
    use_tls: bool = True
    use_ssl: bool = False
    use_credentials: bool = True
    validate_certs: bool = True

class Stripe(BaseModel):
    api_key: str = None
    webhook_secret: str = None


class LicenseConfig(BaseModel):
    """Enterprise license configuration"""
    key: Optional[str] = Field(default=None, description="Enterprise license key (BOW_LICENSE_KEY)")

    @validator('key', pre=True, always=True)
    def load_from_env(cls, v):
        """Auto-load license key from env var if not set or placeholder in config"""
        if not v:
            # No value set, fallback to default env var
            return os.environ.get("BOW_LICENSE_KEY")
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            # Parse env var name from placeholder like ${BOW_LICENSE_KEY2}
            env_var_name = v[2:-1]
            return os.environ.get(env_var_name)
        return v


class DatabaseAuth(BaseModel):
    """Authentication method for the application database.

    provider: 'password' (default) uses the password in the URL.
              'aws_iam' generates short-lived IAM tokens via boto3.
              Future: 'azure_entra', 'gcp_iam'.
    """
    provider: str = "password"
    region: str = ""       # AWS only — e.g. "us-east-1"
    ssl_mode: str = ""     # e.g. "verify-full" (required for IAM auth)
    password: str = ""     # Only used by StaticPasswordProvider when URL has no password


class Database(BaseModel):
    url: str = Field(
        default_factory=lambda: os.getenv(
            "BOW_DATABASE_URL",
            "sqlite:////app/backend/db/app.db"
        )
    )
    # Fields for managed DB with IAM auth (used when auth.provider != 'password')
    host: str = Field(default_factory=lambda: os.getenv("BOW_DATABASE_HOST", ""))
    port: int = Field(default_factory=lambda: int(os.getenv("BOW_DATABASE_PORT", "5432")))
    name: str = Field(default_factory=lambda: os.getenv("BOW_DATABASE_NAME", ""))
    username: str = Field(default_factory=lambda: os.getenv("BOW_DATABASE_USER", ""))
    auth: DatabaseAuth = Field(default_factory=lambda: DatabaseAuth(
        provider=os.getenv("BOW_DATABASE_AUTH_PROVIDER", "password"),
        region=os.getenv("BOW_DATABASE_AUTH_REGION", ""),
        ssl_mode=os.getenv("BOW_DATABASE_SSL_MODE", ""),
    ))

    def get_url(self) -> str:
        """Build the connection URL.

        For 'password' provider, returns the existing url field as-is.
        For IAM providers, constructs the URL from host/port/name/username
        (password is injected at connect time by the auth provider).
        """
        if self.auth.provider == "password":
            return self.url
        # IAM auth — build URL without password; it's injected per-connection
        return f"postgresql://{self.username}@{self.host}:{self.port}/{self.name}"

    @property
    def uses_iam_auth(self) -> bool:
        return self.auth.provider != "password"

def generate_fernet_key():
    # Generate a valid Fernet-compatible key (32 url-safe base64-encoded bytes)
    key = secrets.token_bytes(32)
    return base64.urlsafe_b64encode(key).decode()

class BowConfig(BaseModel):
    deployment: DeploymentConfig = DeploymentConfig()
    base_url: Optional[str] = Field(default="http://0.0.0.0:3000")
    features: FeatureFlags = FeatureFlags()
    auth: AuthConfig = AuthConfig()
    google_oauth: GoogleOAuth = GoogleOAuth()
    oidc_providers: List[OIDCProvider] = []
    default_llm: List[LLMProvider] = []
    smtp_settings: SMTPSettings = None
    encryption_key: str = Field(
        default_factory=generate_fernet_key,
        description="Encryption key for sensitive data",
        env="BOW_ENCRYPTION_KEY"
    )
    stripe: Stripe = Stripe()
    database: Database = Database()
    intercom: Intercom = Intercom()
    telemetry: Telemetry = Telemetry()
    swagger: Swagger = Swagger()
    license: LicenseConfig = LicenseConfig()
    otel: OTELConfig = OTELConfig()

    @validator('encryption_key')
    def validate_encryption_key(cls, v):
        # If the value is empty or still the placeholder, generate a valid key:
        if not v or v.strip() in {"", "${BOW_ENCRYPTION_KEY}"}:
            return generate_fernet_key()
        return v
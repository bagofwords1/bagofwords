from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, validator, field_validator, ConfigDict, AliasGenerator, model_validator
from pydantic.alias_generators import to_camel
import os
import secrets
import base64


class LLMModel(BaseModel):
    model_id: str
    model_name: str
    is_default: bool = False
    is_small_default: bool = False
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


class I18nConfig(BaseModel):
    """Internationalization configuration.

    System-wide default and the allowlist of locales that can be selected by
    organizations. Individual orgs override `default_locale` via
    `OrganizationSettingsConfig.locale`.
    """
    default_locale: str = "en"
    enabled_locales: List[str] = ["en", "es", "he", "fr", "sv", "ar", "ru", "de", "pt", "it"]
    fallback_locale: str = "en"

    @validator("default_locale")
    def _default_in_enabled(cls, v, values):
        enabled = values.get("enabled_locales") or ["en"]
        if v not in enabled:
            raise ValueError(f"default_locale '{v}' must be one of enabled_locales {enabled}")
        return v

    @validator("fallback_locale")
    def _fallback_in_enabled(cls, v, values):
        enabled = values.get("enabled_locales") or ["en"]
        if v not in enabled:
            raise ValueError(f"fallback_locale '{v}' must be one of enabled_locales {enabled}")
        return v


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
        ),
        populate_by_name=True,
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


def brand_for_issuer(issuer: Optional[str]) -> str:
    """Identify the identity provider behind an OIDC issuer URL.

    Returns one of: "microsoft", "google", "custom".
    """
    issuer = (issuer or "").lower()
    if "login.microsoftonline.com" in issuer or "sts.windows.net" in issuer:
        return "microsoft"
    if "accounts.google.com" in issuer:
        return "google"
    return "custom"


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
    # Group sync — sync OIDC group claims into BOW Groups on login
    sync_groups: bool = False
    group_claim: str = "groups"              # claim name in id_token
    resolve_group_names: bool = False        # call Graph API to get display names (Entra returns UUIDs)

    def brand(self) -> str:
        """Which identity provider is behind this entry.

        `name` is a routing slug (it appears in /api/auth/<name>/callback, which
        is registered with the IdP), so it can be anything the operator picked —
        "entra", "corp-sso", "ad". The issuer is what actually tells us who is
        on the other end, and the login page uses this to pick a logo.
        """
        return brand_for_issuer(self.issuer)

    def display_label(self) -> str:
        """Human-readable provider name for the sign-in button.

        An explicit `label` always wins; otherwise a recognized provider gets
        its real product name and anything else falls back to the slug.
        """
        if self.label:
            return self.label
        brand = self.brand()
        if brand == "microsoft":
            return "Microsoft"
        if brand == "google":
            return "Google"
        return (self.name or "").replace("_", " ").replace("-", " ").strip().title() or self.name


class SAMLIdP(BaseModel):
    """Trust material supplied by the operator, never by a login response."""
    metadata_url: Optional[str] = None
    metadata_file: Optional[str] = None
    entity_id: Optional[str] = None
    sso_url: Optional[str] = None
    certificates: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_source(self):
        from urllib.parse import urlsplit
        if self.metadata_url and self.metadata_file:
            raise ValueError("Choose one SAML metadata source")
        if not (self.metadata_url or self.metadata_file or
                (self.entity_id and self.sso_url and self.certificates)):
            raise ValueError("SAML requires metadata or an issuer, SSO URL and certificates")
        for url in (self.metadata_url, self.sso_url):
            if url:
                parsed = urlsplit(url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                    raise ValueError("SAML URLs must use HTTPS without credentials or fragments")
        return self


class SAMLSP(BaseModel):
    entity_id: Optional[str] = None
    certificate_file: Optional[str] = None
    private_key_file: Optional[str] = None
    sign_requests: bool = False
    want_assertions_encrypted: bool = False
    name_id_format: str = "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified"

    @model_validator(mode="after")
    def validate_key_pair(self):
        if bool(self.certificate_file) != bool(self.private_key_file):
            raise ValueError("SAML SP certificate and private key must be supplied together")
        if (self.sign_requests or self.want_assertions_encrypted) and not self.private_key_file:
            raise ValueError("Signed requests/encrypted assertions require an SP key pair")
        return self


class SAMLAttributes(BaseModel):
    # Use full attribute Names from the assertion; short names work for IdPs
    # that emit them. name_id is a reserved selector for the SAML NameID.
    subject: str = "name_id"
    email: str = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"
    name: Optional[str] = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name"


class SAMLProvider(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")
    enabled: bool = False
    label: Optional[str] = None
    organization_id: str = Field(min_length=1)
    idp: SAMLIdP
    sp: SAMLSP = Field(default_factory=SAMLSP)
    attributes: SAMLAttributes = Field(default_factory=SAMLAttributes)
    auto_provision_users: bool = False
    # Explicit operator-approved links for existing organization members.
    # Key = immutable SAML subject; value = existing BOW email. Never auto-link
    # merely because an assertion's email matches an existing account.
    account_links: dict[str, str] = Field(default_factory=dict)


class LDAPConfig(BaseModel):
    """LDAP / Active Directory configuration for group sync and optional bind auth."""
    enabled: bool = False
    url: str = ""                                      # e.g. ldaps://ad.corp.com:636
    bind_dn: Optional[str] = None                      # service account DN
    bind_password: Optional[str] = None                # service account password
    use_ssl: bool = True
    start_tls: bool = False
    ca_certs_file: Optional[str] = None
    # AD-first: each deployment explicitly admits one organization's group.
    organization_id: Optional[str] = None
    admission_group_dn: Optional[str] = None
    kerberos_realm: Optional[str] = None
    allow_local_superuser_login: bool = False
    base_dn: str = ""
    user_search_base: Optional[str] = None             # defaults to base_dn
    user_search_filter: str = "(objectClass=person)"
    user_email_attribute: str = "mail"
    user_name_attribute: str = "displayName"
    group_search_base: Optional[str] = None            # defaults to base_dn
    group_search_filter: str = "(objectClass=group)"
    group_name_attribute: str = "cn"
    group_member_attribute: str = "member"              # "member" (DN) or "memberUid"
    group_member_format: str = "dn"                    # "dn" or "uid"
    sync_interval_minutes: int = 60
    auto_provision_users: bool = False                  # create app user on LDAP login
    connection_timeout: int = 10
    page_size: int = 500

    @validator('bind_password', pre=True, always=True)
    def resolve_env_var(cls, v):
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            return os.environ.get(v[2:-1])
        return v


class SMTPSettings(BaseModel):
    host: str = "smtp.resend.com"
    port: int = 587
    username: Optional[str] = None
    password: Optional[str] = None
    from_name: str = "BOW"
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

# Whether the Fernet key in use was invented at startup rather than supplied by
# the operator. An ephemeral key changes on every restart, so anything written
# with it is unreadable afterwards. Credentials could always be re-entered after
# that; encrypted analytical payloads cannot be re-derived, so
# app.ee.encryption refuses to encrypt while this is set.
_encryption_key_is_ephemeral = False


def mark_encryption_key_ephemeral() -> None:
    global _encryption_key_is_ephemeral
    _encryption_key_is_ephemeral = True


def encryption_key_is_ephemeral() -> bool:
    return _encryption_key_is_ephemeral


def generate_fernet_key():
    # Generate a valid Fernet-compatible key (32 url-safe base64-encoded bytes)
    key = secrets.token_bytes(32)
    return base64.urlsafe_b64encode(key).decode()

class DataEncryptionConfig(BaseModel):
    """Encryption at rest for row-heavy analytical payloads (enterprise).

    Covers query results and the bounded context summaries derived from them:
    ``Step.data``, ``StepUserResult.data``, ``Entity.data``,
    ``EntityUserResult.data`` and ``ToolExecution.result_json`` — plus
    everything that restates those rows: conversation text
    (``Completion.prompt``/``completion``, the ``prompt_text`` columns),
    agent context (``ContextSnapshot.context_view_json``,
    ``ReportContextState.summary_json``), generated artifact content
    (``ArtifactVersion``/``ArtifactRelease.content``), the parameter values a
    snapshot was run with (``applied_params``) and ``Entity.code``. Credentials
    have always been encrypted separately; this extends the same protection to
    the data those credentials fetched, reusing the same ``encryption_key``.

    Defaults to on: on a licensed instance the safe posture is encrypted. It is
    inert without the enterprise ``data_encryption`` feature, and switching it
    off only stops *new* writes from being encrypted — already-stored
    ciphertext is always still decrypted on read.
    """
    enabled: bool = True

    @validator("enabled", pre=True)
    def _default_when_unset(cls, v):
        # An operator may wire this to an env var; an unresolved placeholder
        # arrives as None. Treat "not specified" as the default rather than
        # failing config validation and taking the whole instance down.
        return True if v is None else v


class SecurityConfig(BaseModel):
    # When true, admin-configured outbound URLs (git remotes, LLM base_url,
    # audit-stream endpoints) must resolve to public addresses. Off by default
    # because self-hosted installs point these at internal hosts; cloud
    # metadata / link-local addresses are blocked either way.
    block_private_outbound: bool = False
    # Directory prefixes a network_dir connection's root_path may resolve
    # into (realpath, symlinks followed), e.g. ["/mnt", "/srv/shares"]. When
    # set it is enforced strictly. Unset (default) keeps existing installs
    # working and only refuses system dirs (/, /etc, /proc, /root, /usr, …)
    # and the app's own dirs — see network_dir_client.check_network_dir_root.
    network_dir_allowed_roots: Optional[List[str]] = None
    # Python 3.13 validates outbound TLS chains in OpenSSL's strict X.509 mode,
    # which rejects any CA-issued certificate lacking an Authority Key
    # Identifier ("certificate verify failed: Missing Authority Key
    # Identifier"). TLS-inspecting corporate proxies that re-sign traffic with a
    # hand-made CA are the usual offender; every outbound HTTPS call (OIDC
    # discovery, Graph, LLM providers) then fails. Set false to fall back to
    # the pre-3.13 checks: chains are still verified against the trust store and
    # hostnames still checked, only the extension-presence rules are dropped.
    # Env: BOW_TLS_STRICT_X509. Only an explicit false/0/no/off relaxes; a typo
    # keeps strict mode. Prefer fixing the proxy CA; this is the bridge.
    tls_strict_x509: bool = Field(
        default_factory=lambda: (os.getenv("BOW_TLS_STRICT_X509") or "").strip().lower()
        not in ("0", "false", "no", "off")
    )
    # Peers whose X-Forwarded-For is believed when resolving the client address
    # recorded in audit events (CIDR list). None = loopback and RFC 1918/ULA
    # ranges, which covers a reverse proxy on the same host or Docker network.
    # Set it to exactly your proxy addresses on a shared network, or [] to
    # record the TCP peer only. Env: BOW_TRUSTED_PROXIES (comma separated).
    trusted_proxies: Optional[List[str]] = Field(
        default_factory=lambda: (
            [p.strip() for p in os.getenv("BOW_TRUSTED_PROXIES").split(",") if p.strip()]
            if os.getenv("BOW_TRUSTED_PROXIES") is not None else None
        )
    )


class McpAppsConfig(BaseModel):
    # Origin that serves the MCP Apps sandbox page (e.g.
    # https://bow-sandbox.example.com, pointing at this same backend). The
    # MCP Apps spec requires the app frame to run on an origin other than the
    # host's. Unset: the frame runs in an opaque (sandboxed, origin-less)
    # context instead — just as isolated, but apps cannot use localStorage or
    # cookies. Env: BOW_MCP_APPS_SANDBOX_ORIGIN.
    sandbox_origin: Optional[str] = Field(
        default_factory=lambda: (os.getenv("BOW_MCP_APPS_SANDBOX_ORIGIN") or "").strip().rstrip("/") or None
    )


class AuditPlatformStream(BaseModel):
    """Restricted collector for events that belong to no organization
    (unknown-account login failures, platform operations). Same destination
    types and fields as an organization stream; secrets are read from this
    config (use ${ENV} placeholders), never stored through the API."""
    enabled: bool = False
    destination: str = "https"
    config: Dict[str, Any] = Field(default_factory=dict)
    secrets: Dict[str, Any] = Field(default_factory=dict)
    action_filter: Optional[List[str]] = None


class AuditConfig(BaseModel):
    """Audit evidence controls (docs/design/audit-event-contract.md)."""
    # Bound on one event's details payload; beyond it the payload becomes an
    # explicit truncation marker. Env: BOW_AUDIT_MAX_DETAILS_BYTES.
    max_details_bytes: int = Field(default_factory=lambda: int(os.getenv("BOW_AUDIT_MAX_DETAILS_BYTES", "65536")))
    # Bound on the executed-query payload of one agent/tool event; every query
    # is kept, the longest are shortened with a marker. Env: BOW_AUDIT_MAX_QUERY_PAYLOAD_BYTES.
    max_query_payload_bytes: int = Field(default_factory=lambda: int(os.getenv("BOW_AUDIT_MAX_QUERY_PAYLOAD_BYTES", "32768")))
    # Heartbeat event written for every organization with an active stream so a
    # collector can tell a silent producer from an idle one. 0 disables.
    heartbeat_interval_seconds: int = Field(default_factory=lambda: int(os.getenv("BOW_AUDIT_HEARTBEAT_INTERVAL_SECONDS", "300")))
    # Consecutive retryable delivery failures after which a stream is reported
    # as degraded (audit event + admin e-mail) while it keeps retrying.
    degraded_after_failures: int = Field(default_factory=lambda: int(os.getenv("BOW_AUDIT_DEGRADED_AFTER_FAILURES", "5")))
    # Retention. None/0 = keep forever. Rows older than this are purged by the
    # daily job unless they are still undelivered to an active stream or a
    # legal hold is set. Env: BOW_AUDIT_RETENTION_DAYS / BOW_AUDIT_LEGAL_HOLD.
    retention_days: Optional[int] = Field(default_factory=lambda: int(os.getenv("BOW_AUDIT_RETENTION_DAYS")) if os.getenv("BOW_AUDIT_RETENTION_DAYS") else None)
    legal_hold: bool = Field(default_factory=lambda: (os.getenv("BOW_AUDIT_LEGAL_HOLD") or "").strip().lower() in ("1", "true", "yes", "on"))
    platform_stream: AuditPlatformStream = AuditPlatformStream()


def _storage_env(name: str, default: str = "") -> str:
    return os.getenv(f"BOW_STORAGE_{name}", default).strip()


class S3StorageConfig(BaseModel):
    """S3 or any S3-compatible store (MinIO, R2, Wasabi, Ceph, StorageGRID...).

    auth: 'default'     boto3 default chain (IRSA / Pod Identity / instance
                        role / env vars / profile). Recommended on AWS.
          'static'      access_key_id + secret_access_key (+ session_token).
          'assume_role' STS assume-role of role_arn on top of the default chain,
                        or of the static keys when they are set.
    """
    # Validate env-derived defaults too, not only values from the YAML.
    model_config = ConfigDict(validate_default=True)

    bucket: str = Field(default_factory=lambda: _storage_env("S3_BUCKET"))
    region: str = Field(default_factory=lambda: _storage_env("S3_REGION"))
    endpoint_url: str = Field(default_factory=lambda: _storage_env("S3_ENDPOINT_URL"))
    addressing_style: str = Field(default_factory=lambda: _storage_env("S3_ADDRESSING_STYLE", "auto"))
    auth: str = Field(default_factory=lambda: _storage_env("S3_AUTH", "default"))
    access_key_id: str = Field(default_factory=lambda: _storage_env("S3_ACCESS_KEY_ID"))
    secret_access_key: str = Field(default_factory=lambda: _storage_env("S3_SECRET_ACCESS_KEY"))
    session_token: str = Field(default_factory=lambda: _storage_env("S3_SESSION_TOKEN"))
    role_arn: str = Field(default_factory=lambda: _storage_env("S3_ROLE_ARN"))
    external_id: str = Field(default_factory=lambda: _storage_env("S3_EXTERNAL_ID"))
    sse: str = Field(default_factory=lambda: _storage_env("S3_SSE"))  # "" | AES256 | aws:kms
    kms_key_id: str = Field(default_factory=lambda: _storage_env("S3_KMS_KEY_ID"))

    @field_validator("auth")
    @classmethod
    def _check_auth(cls, v):
        v = (v or "default").strip().lower()
        if v not in {"default", "static", "assume_role"}:
            raise ValueError("storage.s3.auth must be one of: default, static, assume_role")
        return v

    @field_validator("addressing_style")
    @classmethod
    def _check_addressing(cls, v):
        v = (v or "auto").strip().lower()
        if v not in {"auto", "path", "virtual"}:
            raise ValueError("storage.s3.addressing_style must be one of: auto, path, virtual")
        return v


class AzureStorageConfig(BaseModel):
    """Azure Blob Storage.

    auth: 'default'           DefaultAzureCredential (AKS Workload Identity,
                              Managed Identity, env, Azure CLI). Recommended.
          'service_principal' tenant_id + client_id + client_secret.
          'connection_string' connection_string (dev / Azurite).
          'sas'               account_url + container-scoped sas_token.
    """
    # Validate env-derived defaults too, not only values from the YAML.
    model_config = ConfigDict(validate_default=True)

    account_url: str = Field(default_factory=lambda: _storage_env("AZURE_ACCOUNT_URL"))
    container: str = Field(default_factory=lambda: _storage_env("AZURE_CONTAINER"))
    auth: str = Field(default_factory=lambda: _storage_env("AZURE_AUTH", "default"))
    tenant_id: str = Field(default_factory=lambda: _storage_env("AZURE_TENANT_ID"))
    client_id: str = Field(default_factory=lambda: _storage_env("AZURE_CLIENT_ID"))
    client_secret: str = Field(default_factory=lambda: _storage_env("AZURE_CLIENT_SECRET"))
    connection_string: str = Field(default_factory=lambda: _storage_env("AZURE_CONNECTION_STRING"))
    sas_token: str = Field(default_factory=lambda: _storage_env("AZURE_SAS_TOKEN"))

    @field_validator("auth")
    @classmethod
    def _check_auth(cls, v):
        v = (v or "default").strip().lower()
        if v not in {"default", "service_principal", "connection_string", "sas"}:
            raise ValueError(
                "storage.azure.auth must be one of: default, service_principal, connection_string, sas"
            )
        return v


class StorageConfig(BaseModel):
    """Where durable files live (uploads, exports, thumbnails, branding...).

    backend: 'local' (default) keeps everything on the local disk under
             backend/uploads/, exactly as before.
             's3' / 'azure' make the object store the source of truth; the
             local uploads/ directory becomes a per-pod cache that is filled
             on demand, so any replica (or a restarted pod) sees every file.
    prefix:  key prefix inside the bucket/container, e.g. "bow/".
    """
    # Validate env-derived defaults too, not only values from the YAML.
    model_config = ConfigDict(validate_default=True)

    backend: str = Field(default_factory=lambda: _storage_env("BACKEND", "local"))
    prefix: str = Field(default_factory=lambda: _storage_env("PREFIX"))
    s3: S3StorageConfig = Field(default_factory=S3StorageConfig)
    azure: AzureStorageConfig = Field(default_factory=AzureStorageConfig)

    @field_validator("backend")
    @classmethod
    def _check_backend(cls, v):
        v = (v or "local").strip().lower()
        if v not in {"local", "s3", "azure"}:
            raise ValueError("storage.backend must be one of: local, s3, azure")
        return v

    @field_validator("prefix")
    @classmethod
    def _normalize_prefix(cls, v):
        v = (v or "").strip().strip("/")
        if ".." in v.split("/"):
            raise ValueError("storage.prefix must not contain '..'")
        return f"{v}/" if v else ""


class BowConfig(BaseModel):
    deployment: DeploymentConfig = DeploymentConfig()
    base_url: Optional[str] = Field(default="http://0.0.0.0:3000")
    mcp_public_url: Optional[str] = None
    features: FeatureFlags = FeatureFlags()
    auth: AuthConfig = AuthConfig()
    google_oauth: GoogleOAuth = GoogleOAuth()
    ldap: LDAPConfig = LDAPConfig()
    oidc_providers: List[OIDCProvider] = []
    saml_providers: List[SAMLProvider] = Field(default_factory=list)
    default_llm: List[LLMProvider] = []
    smtp_settings: SMTPSettings = None
    # Resolved by ``validate_encryption_key`` below (env var, then generated).
    # ``validate_default=True`` so an omitted key goes through the same path as
    # an empty or placeholder one instead of silently minting a per-process key.
    encryption_key: str = Field(
        default="",
        validate_default=True,
        description="Encryption key for sensitive data",
    )
    data_encryption: DataEncryptionConfig = DataEncryptionConfig()
    stripe: Stripe = Stripe()
    database: Database = Database()
    intercom: Intercom = Intercom()
    telemetry: Telemetry = Telemetry()
    swagger: Swagger = Swagger()
    license: LicenseConfig = LicenseConfig()
    otel: OTELConfig = OTELConfig()
    i18n: I18nConfig = I18nConfig()
    security: SecurityConfig = SecurityConfig()
    audit: AuditConfig = AuditConfig()
    storage: StorageConfig = Field(default_factory=StorageConfig)
    mcp_apps: McpAppsConfig = Field(default_factory=McpAppsConfig)

    @model_validator(mode="after")
    def validate_saml_providers(self):
        from urllib.parse import urlsplit
        names = [p.name for p in self.saml_providers]
        if len(names) != len(set(names)) or set(names) & ({"google"} | {p.name for p in self.oidc_providers}):
            raise ValueError("SAML provider names must be unique across login providers")
        if any(p.enabled for p in self.saml_providers):
            url = urlsplit(self.base_url or "")
            if url.scheme != "https" or not url.hostname or url.query or url.fragment or url.username or url.password or url.path not in ("", "/"):
                raise ValueError("Enabled SAML requires an HTTPS base_url origin")
        return self

    @field_validator('encryption_key', mode='before')
    @classmethod
    def validate_encryption_key(cls, v):
        # Resolution order:
        #   1. an explicit literal in bow-config.yaml
        #   2. the BOW_ENCRYPTION_KEY env var (what start.sh / k8s Secrets set)
        #   3. a freshly generated key, with a loud warning
        # The env var lookup lives here (not only in the ``${VAR}`` substitution
        # done by the config loader) so a bow-config.yaml that omits
        # ``encryption_key`` entirely still honors the key the operator provided.
        # An ephemeral, process-local key is fine for a throwaway run but is a
        # footgun in any persistent deployment: it changes on every restart (and
        # differs per uvicorn worker), so everything encrypted with it (LLM
        # provider keys, data-source credentials) becomes undecryptable.
        if isinstance(v, str) and v.strip() not in {"", "${BOW_ENCRYPTION_KEY}"}:
            return v
        from_env = os.environ.get("BOW_ENCRYPTION_KEY", "").strip()
        if from_env and from_env != "${BOW_ENCRYPTION_KEY}":
            return from_env
        import logging
        logging.getLogger(__name__).warning(
            "BOW_ENCRYPTION_KEY is not set — generating a random, in-memory "
            "encryption key. It will change on the next restart, making all "
            "currently-encrypted credentials undecryptable. Set a stable "
            "BOW_ENCRYPTION_KEY for any non-throwaway deployment."
        )
        # Invented here, so it changes on the next restart. Flagged so payload
        # encryption stays off rather than writing data that this process alone
        # can read (see app/ee/encryption/types.py).
        mark_encryption_key_ephemeral()
        return generate_fernet_key()

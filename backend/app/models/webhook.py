import secrets

from cryptography.fernet import Fernet
from sqlalchemy import Column, String, ForeignKey, Boolean, Text, DateTime
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema
from app.settings.config import settings


class Webhook(BaseSchema):
    """Per-report inbound webhook.

    External systems (GitHub, Jira, generic services) POST events to
    ``/webhooks/{token}``. Each webhook is HMAC- or token-verified with its own
    signing key, then the event is shown in the report chat and (optionally)
    judged by the AI classifier which decides whether the agent should act.
    """

    __tablename__ = 'webhooks'

    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False, index=True)
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False)

    name = Column(String, nullable=False, default='Webhook')
    # Public, unguessable path segment used in the delivery URL.
    token = Column(String, nullable=False, unique=True, index=True)
    # Fernet-encrypted signing key — HMAC key, bearer token, or url token depending on auth_mode.
    secret_encrypted = Column(String, nullable=False)

    source = Column(String, nullable=False, default='generic')  # github | jira | generic
    auth_mode = Column(String, nullable=False, default='hmac')  # hmac | token | url_token
    auth_header_name = Column(String, nullable=True, default='Authorization')  # for token mode

    classify_enabled = Column(Boolean, nullable=False, default=True)
    classifier_prompt = Column(Text, nullable=True, default=None)

    is_active = Column(Boolean, nullable=False, default=True)
    last_delivery_at = Column(DateTime, nullable=True, default=None)

    report = relationship("Report", lazy='select')
    user = relationship("User", lazy='select')

    # ---- secret helpers (mirror LLMProvider's Fernet usage) ----

    @staticmethod
    def generate_token() -> str:
        return f"whk_{secrets.token_urlsafe(24)}"

    @staticmethod
    def generate_secret() -> str:
        return f"whsec_{secrets.token_urlsafe(32)}"

    def set_secret(self, secret: str) -> None:
        fernet = Fernet(settings.bow_config.encryption_key)
        self.secret_encrypted = fernet.encrypt(secret.encode()).decode()

    def get_secret(self) -> str:
        fernet = Fernet(settings.bow_config.encryption_key)
        return fernet.decrypt(self.secret_encrypted.encode()).decode()

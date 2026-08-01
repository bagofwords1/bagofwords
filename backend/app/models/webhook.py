import secrets

from cryptography.fernet import Fernet
from sqlalchemy import Column, String, ForeignKey, Boolean, Text, DateTime, JSON
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema
from app.models.webhook_data_source_association import webhook_data_source_association  # noqa: F401 (registers the M2M table)
from app.settings.config import settings


class Webhook(BaseSchema):
    """Inbound webhook — two scopes on one entity:

    - **Report-bound** (``report_id`` set): external systems POST events to
      ``/webhooks/{token}`` and the event lands in that report's chat, with the
      optional AI classifier deciding whether the agent should act. The
      original behavior.
    - **Trigger** (``report_id`` NULL): a user-owned standing trigger. Each
      accepted delivery SPAWNS a new session (report) owned by the webhook's
      creator, attached to the trigger's agents (``data_sources``), running
      with the trigger's ``mode``/``model_id`` on the creator's access and
      quota. ``task_template`` is the standing instruction; the event payload
      is appended as untrusted data. Identity is preset at creation — unlike
      external platforms, nothing is resolved from the sender.
    """

    __tablename__ = 'webhooks'

    # NULL = standalone trigger (spawn mode); set = report-bound webhook.
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=True, index=True)
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False)
    # Optional project home (spawn mode). Every session this trigger spawns is
    # created inside it, and the trigger itself shows up in the project's
    # automations. Nullable = the trigger lives at the root, as before.
    project_id = Column(String(36), ForeignKey('projects.id'), nullable=True, index=True)

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

    # ── Trigger run spec (spawn mode only; mirrors Prompt's execution spec) ──
    # Standing instruction for spawned runs. With a template the classifier only
    # gates WHETHER to act; without one it authors the task per event (legacy).
    task_template = Column(Text, nullable=True, default=None)
    mode = Column(String, nullable=False, default='chat')  # 'chat' | 'deep'
    model_id = Column(String(36), nullable=True, default=None)  # LLM override; null = org default

    is_active = Column(Boolean, nullable=False, default=True)
    last_delivery_at = Column(DateTime, nullable=True, default=None)
    # Last verified delivery, captured whether or not the trigger is active:
    # {received_at, summary, headers, raw, acted}. Lets the setup UI confirm
    # "your event arrived" before the trigger is switched on, and gives the
    # owner a sample payload to write the task against. Auth-bearing headers
    # are stripped (see webhook_service._safe_headers).
    last_event = Column(JSON, nullable=True, default=None)

    report = relationship("Report", lazy='select', foreign_keys=[report_id])
    # selectin, not select: the schema is built outside the async context, so a
    # lazy load here would blow up rather than silently query.
    user = relationship("User", lazy='selectin')
    project = relationship("Project", lazy='selectin', foreign_keys=[project_id])
    # Agents attached to every spawned session (spawn mode only).
    data_sources = relationship(
        "DataSource",
        secondary="webhook_data_source_association",
        lazy="selectin",
    )

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

from sqlalchemy import Column, String, Boolean, JSON, DateTime, Text, Integer, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
from cryptography.fernet import Fernet
from app.settings.config import settings
import json


class Connection(BaseSchema):
    """
    Represents a database connection with credentials and configuration.
    A Connection can be associated with multiple DataSources (Domains) via M:N relationship.
    """
    __tablename__ = "connections"
    __table_args__ = (
        UniqueConstraint('organization_id', 'name', name='uq_connections_org_name'),
    )

    name = Column(String, nullable=False)
    type = Column(String, nullable=False)  # e.g., 'snowflake', 'postgres', 'bigquery'
    config = Column(JSON, nullable=False)  # Non-secret connection parameters
    credentials = Column(Text, nullable=True)  # Encrypted credentials
    
    is_active = Column(Boolean, nullable=False, default=True)
    last_synced_at = Column(DateTime, nullable=True)
    
    # Connection test cache - stores last test result to avoid repeated slow tests
    last_connection_status = Column(String, nullable=True)  # "success", "not_connected", "offline"
    last_connection_checked_at = Column(DateTime, nullable=True)
    
    # Authentication policy
    auth_policy = Column(String, nullable=False, default="system_only")  # system_only, user_required
    allowed_user_auth_modes = Column(JSON, nullable=True, default=None)

    # Scheduled schema auto-reload (enterprise feature `scheduled_reindex`).
    # A background sweeper periodically re-indexes the shared catalog so tables
    # stay fresh without a manual reindex. Per-connection so admins can tune
    # cadence (or disable) per source. NULL interval falls back to the default.
    auto_reindex_enabled = Column(Boolean, nullable=False, default=True)
    reindex_interval_hours = Column(Integer, nullable=True, default=None)

    # The schedule is EITHER a recurring interval OR a fixed time-of-day:
    #   * mode == "interval": fire every `reindex_interval_minutes` (10m floor).
    #   * mode == "time":     fire once a day at `reindex_at_time` ("HH:MM"),
    #                         interpreted in the org's timezone (UTC fallback).
    # `reindex_interval_minutes` supersedes the legacy `reindex_interval_hours`
    # (kept for back-compat / backfill); minutes is the source of truth.
    reindex_schedule_mode = Column(String, nullable=False, default="interval")  # interval | time
    reindex_interval_minutes = Column(Integer, nullable=True, default=None)
    reindex_at_time = Column(String, nullable=True, default=None)  # "HH:MM" (24h)
    # Failure backoff / "wait for next attempt" state for the sweeper. On a
    # failed (or skipped) background reindex we set next_retry_at so we don't
    # hammer the source every tick — user_required catalogs heal on user login
    # in the meantime. Cleared on a successful index.
    next_retry_at = Column(DateTime, nullable=True, default=None)
    last_reindex_error = Column(Text, nullable=True, default=None)

    # Default cadence when no interval is configured (every 12 hours).
    DEFAULT_REINDEX_INTERVAL_HOURS = 12
    DEFAULT_REINDEX_INTERVAL_MINUTES = 12 * 60
    # Hard floor on interval cadence — guards against runaway tight loops.
    MIN_REINDEX_INTERVAL_MINUTES = 10

    @property
    def effective_reindex_interval_minutes(self) -> int:
        """Resolved interval cadence in minutes — the per-connection override or
        the default, with a hard floor. Prefers the minutes column; falls back
        to the legacy hours column for rows written before the minutes split."""
        val = self.reindex_interval_minutes
        if val is None or val <= 0:
            legacy = self.reindex_interval_hours
            if legacy and legacy > 0:
                val = legacy * 60
            else:
                val = self.DEFAULT_REINDEX_INTERVAL_MINUTES
        return max(self.MIN_REINDEX_INTERVAL_MINUTES, val)

    @property
    def effective_reindex_interval_hours(self) -> int:
        """Back-compat shim — resolved cadence in (rounded-up) hours."""
        minutes = self.effective_reindex_interval_minutes
        return max(1, (minutes + 59) // 60)

    # Organization ownership
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False)
    organization = relationship("Organization", back_populates="connections")
    
    # Relationships
    connection_tables = relationship(
        "ConnectionTable",
        back_populates="connection",
        cascade="all, delete-orphan",
        passive_deletes=False,
    )
    
    # M:N relationship to DataSource (Domain)
    data_sources = relationship(
        "DataSource",
        secondary="domain_connection",
        back_populates="connections",
        lazy="selectin"
    )
    
    # User-level credentials for this connection
    user_credentials = relationship(
        "UserConnectionCredentials",
        back_populates="connection",
        cascade="all, delete-orphan"
    )
    
    # User-level table overlays
    user_tables = relationship(
        "UserConnectionTable",
        back_populates="connection",
        cascade="all, delete-orphan"
    )

    # MCP/API tool discovery
    connection_tools = relationship(
        "ConnectionTool",
        back_populates="connection",
        cascade="all, delete-orphan",
        passive_deletes=False,
    )

    # User-level tool overlays
    user_tools = relationship(
        "UserConnectionTool",
        back_populates="connection",
        cascade="all, delete-orphan",
    )

    # Background schema ingestion history (one row per refresh attempt)
    indexings = relationship(
        "ConnectionIndexing",
        back_populates="connection",
        cascade="all, delete-orphan",
        order_by="ConnectionIndexing.created_at.desc()",
    )

    def get_client(self):
        """Instantiate and return the appropriate database client."""
        try:
            from app.schemas.data_source_registry import resolve_client_class
            ClientClass = resolve_client_class(self.type)

            # Parse config if it's a string
            config = json.loads(self.config) if isinstance(self.config, str) else self.config
            client_params = config.copy()
            
            # Only decrypt and merge credentials if they exist
            if self.credentials:
                decrypted_credentials = self.decrypt_credentials()
                client_params.update(decrypted_credentials)
            
            # Remove non-client params
            if "auth_type" in client_params:
                del client_params["auth_type"]
            if "demo_id" in client_params:
                del client_params["demo_id"]
            
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"Client params for {self.type}")
            
            return ClientClass(**client_params)
        except (ImportError, AttributeError) as e:
            raise ValueError(f"Unable to load data source client for {self.type}: {str(e)}")

    def get_credentials(self):
        """Get decrypted credentials based on auth policy."""
        if self.auth_policy == "system_only":
            return self.decrypt_credentials()
        elif self.auth_policy == "user_required":
            return None
        else:
            raise ValueError(f"Invalid auth policy: {self.auth_policy}")

    def encrypt_credentials(self, credentials: dict):
        """Encrypt credentials before storing."""
        fernet = Fernet(settings.bow_config.encryption_key)
        self.credentials = fernet.encrypt(json.dumps(credentials).encode()).decode()

    def decrypt_credentials(self) -> dict:
        """Decrypt stored credentials."""
        if not self.credentials:
            return {}
        fernet = Fernet(settings.bow_config.encryption_key)
        return json.loads(fernet.decrypt(self.credentials.encode()).decode())


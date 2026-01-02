from sqlalchemy import Column, String, Boolean, JSON, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
from importlib import import_module
from cryptography.fernet import Fernet
from app.settings.config import settings
import json


class Connection(BaseSchema):
    """
    Represents a database connection with credentials and configuration.
    A Connection can be associated with multiple DataSources (Domains) via M:N relationship.
    """
    __tablename__ = "connections"

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

    def get_client(self):
        """Instantiate and return the appropriate database client."""
        try:
            module_name = f"app.data_sources.clients.{self.type.lower()}_client"
            # Capitalize the first letter of each word without changing the rest
            title = "".join(word[:1].upper() + word[1:] for word in self.type.split("_"))
            class_name = f"{title}Client"
            
            module = import_module(module_name)
            ClientClass = getattr(module, class_name)
            
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


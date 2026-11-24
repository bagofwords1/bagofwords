from sqlalchemy import Column, String, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


class UserConnectorPreference(BaseSchema):
    """Per-user on/off choice for one personal connector in the chat composer.

    A connector (Gmail, Teams, personal Drive, a per-user MCP such as GitHub)
    is joined into every run automatically — see
    ``agent_focus_common.resolve_run_agents``. When the person turns it OFF in
    the connectors menu, that connector contributes no client, no context and
    no tool definitions, so its tools cost nothing and cannot be called.

    The choice is per ``(user, data_source)``; the workspace is implied by the
    data source's organization. Absence of a row means ON — a newly connected
    connector works as before, and only an explicit OFF is persisted. Reads go
    through ``connector_preference_service.get_disabled_connector_ids`` so the
    hot path loads one indexed query per run.
    """

    __tablename__ = "user_connector_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "data_source_id", name="uq_ucp_user_ds"),
    )

    user_id = Column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id = Column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    data_source_id = Column(
        String(36), ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enabled = Column(Boolean, nullable=False, default=True)

    user = relationship("User", lazy="selectin")
    data_source = relationship("DataSource", lazy="selectin")

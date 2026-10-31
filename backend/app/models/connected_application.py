"""One external application identity; OAuth and embedding are optional capabilities."""
from sqlalchemy import JSON, Boolean, Column, ForeignKey, String, select
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import declared_attr, relationship

from app.models.base import BaseSchema


# An instruction targets a connected application by listing this value in its
# applicable_channels, next to the fixed channels ('app', 'slack', ...).
APPLICATION_CHANNEL_PREFIX = "connected_app:"


def application_channel(application_id) -> str:
    return f"{APPLICATION_CHANNEL_PREFIX}{application_id}"


class ConnectedApplication(BaseSchema):
    __tablename__ = "connected_applications"

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    # NULL: all agents the viewer may use (legacy OAuth behavior). []: none.
    agent_ids = Column(JSON, nullable=True)
    created_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)


def _application_field(name):
    """Keep legacy service attributes while storing shared metadata only once."""
    @hybrid_property
    def value(self):
        return getattr(self.application, name) if self.application else None

    @value.setter
    def value(self, val):
        if self.application is None:
            self.application = ConnectedApplication()
        setattr(self.application, name, val)

    @value.expression
    def value(cls):
        return select(getattr(ConnectedApplication, name)).where(
            ConnectedApplication.id == cls.id).scalar_subquery()
    return value


class ApplicationConfiguration:
    @declared_attr
    def id(cls):
        return Column(String(36), ForeignKey("connected_applications.id", ondelete="CASCADE"), primary_key=True)

    @declared_attr
    def application(cls):
        # The non-null FK guarantees a parent. An inner join also lets PostgreSQL
        # lock both rows during token issuance (FOR UPDATE rejects outer joins).
        return relationship(ConnectedApplication, lazy="joined", innerjoin=True)

    organization_id = _application_field("organization_id")
    name = _application_field("name")
    enabled = _application_field("enabled")
    created_by_user_id = _application_field("created_by_user_id")
    created_at = _application_field("created_at")
    updated_at = _application_field("updated_at")
    deleted_at = _application_field("deleted_at")

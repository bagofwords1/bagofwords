"""Data edge agent registration — design D2.

One row per edge agent per org, updated in place by each advertisement the
control plane receives over NATS (A10). The advertisement is stored whole,
including connections the handler rejected, so the Data Tunnels UI can explain
why a connection is missing.
"""
from sqlalchemy import Column, String, DateTime, JSON, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


class DataEdgeAgent(BaseSchema):
    __tablename__ = "data_edge_agents"
    __table_args__ = (
        # One row per agent per org, updated in place by each advertisement.
        # The constraint is what makes the handler's upsert safe under the
        # timer-driven re-advertisement in A10.
        UniqueConstraint("organization_id", "edge_agent_id", name="uq_data_edge_agents_org_agent"),
    )

    edge_agent_id = Column(String(255), nullable=False)
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)

    status = Column(String(50), default="offline")   # online / offline / stale
    last_connected_at = Column(DateTime, nullable=True)
    last_heartbeat_at = Column(DateTime, nullable=True)
    client_version = Column(String(50), nullable=True)
    label = Column(String(255), nullable=True)          # advertised edge_agent_name

    # Last advertisement, stored whole — including connections the handler
    # rejected — with a per-connection outcome. See D2 for the shape.
    last_advertisement = Column(JSON, nullable=True)
    last_advertised_at = Column(DateTime, nullable=True)

    organization = relationship("Organization")

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


class UsagePolicy(BaseSchema):
    __tablename__ = "usage_policies"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_usage_policies_org_name"),
    )

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    monthly_token_limit = Column(Integer, nullable=True)
    monthly_query_limit = Column(Integer, nullable=True)
    monthly_data_bytes_limit = Column(BigInteger, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)

    organization = relationship("Organization")
    assignments = relationship(
        "UsagePolicyAssignment",
        back_populates="policy",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    connection_overrides = relationship(
        "UsagePolicyConnectionOverride",
        back_populates="policy",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class UsagePolicyAssignment(BaseSchema):
    __tablename__ = "usage_policy_assignments"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "policy_id",
            "principal_type",
            "principal_id",
            name="uq_usage_policy_assignment",
        ),
    )

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    policy_id = Column(String(36), ForeignKey("usage_policies.id", ondelete="CASCADE"), nullable=False)
    principal_type = Column(String, nullable=False)
    principal_id = Column(String(36), nullable=False)

    policy = relationship("UsagePolicy", back_populates="assignments")


class UsagePolicyConnectionOverride(BaseSchema):
    __tablename__ = "usage_policy_connection_overrides"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "policy_id",
            "connection_id",
            name="uq_usage_policy_connection_override",
        ),
    )

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    policy_id = Column(String(36), ForeignKey("usage_policies.id", ondelete="CASCADE"), nullable=False)
    connection_id = Column(String(36), ForeignKey("connections.id", ondelete="CASCADE"), nullable=False)
    monthly_query_limit = Column(Integer, nullable=True)
    monthly_data_bytes_limit = Column(BigInteger, nullable=True)

    policy = relationship("UsagePolicy", back_populates="connection_overrides")
    connection = relationship("Connection")


class UsageCounter(BaseSchema):
    __tablename__ = "usage_counters"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "user_id",
            "metric",
            "scope_type",
            "scope_ref_id",
            "window_start",
            name="uq_usage_counter_window",
        ),
    )

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    metric = Column(String, nullable=False)
    scope_type = Column(String, nullable=False, default="organization")
    scope_ref_id = Column(String(36), nullable=False, default="")
    window_start = Column(DateTime, nullable=False)
    window_end = Column(DateTime, nullable=False)
    used = Column(BigInteger, nullable=False, default=0)


class UsageEvent(BaseSchema):
    __tablename__ = "usage_events"

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    policy_id = Column(String(36), ForeignKey("usage_policies.id", ondelete="SET NULL"), nullable=True)
    metric = Column(String, nullable=False)
    amount = Column(BigInteger, nullable=False)
    scope_type = Column(String, nullable=False, default="organization")
    scope_ref_id = Column(String(36), nullable=False, default="")
    source = Column(String, nullable=True)
    source_ref_id = Column(String(36), nullable=True)
    usage_metadata = Column(JSON, nullable=True)

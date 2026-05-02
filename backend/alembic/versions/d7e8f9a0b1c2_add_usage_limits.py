"""add usage limit policies

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-05-02 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d7e8f9a0b1c2"
down_revision: Union[str, None] = "c6d7e8f9a0b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "usage_policies",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("monthly_token_limit", sa.Integer(), nullable=True),
        sa.Column("monthly_query_limit", sa.Integer(), nullable=True),
        sa.Column("monthly_data_bytes_limit", sa.BigInteger(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id"),
        sa.UniqueConstraint("organization_id", "name", name="uq_usage_policies_org_name"),
    )
    op.create_index(op.f("ix_usage_policies_id"), "usage_policies", ["id"], unique=False)

    op.create_table(
        "usage_policy_assignments",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("policy_id", sa.String(36), nullable=False),
        sa.Column("principal_type", sa.String(), nullable=False),
        sa.Column("principal_id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["policy_id"], ["usage_policies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "policy_id",
            "principal_type",
            "principal_id",
            name="uq_usage_policy_assignment",
        ),
    )
    op.create_index(op.f("ix_usage_policy_assignments_id"), "usage_policy_assignments", ["id"], unique=False)

    op.create_table(
        "usage_policy_connection_overrides",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("policy_id", sa.String(36), nullable=False),
        sa.Column("connection_id", sa.String(36), nullable=False),
        sa.Column("monthly_query_limit", sa.Integer(), nullable=True),
        sa.Column("monthly_data_bytes_limit", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["policy_id"], ["usage_policies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "policy_id",
            "connection_id",
            name="uq_usage_policy_connection_override",
        ),
    )
    op.create_index(
        op.f("ix_usage_policy_connection_overrides_id"),
        "usage_policy_connection_overrides",
        ["id"],
        unique=False,
    )

    op.create_table(
        "usage_counters",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("metric", sa.String(), nullable=False),
        sa.Column("scope_type", sa.String(), nullable=False),
        sa.Column("scope_ref_id", sa.String(36), nullable=False),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("window_end", sa.DateTime(), nullable=False),
        sa.Column("used", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            "metric",
            "scope_type",
            "scope_ref_id",
            "window_start",
            name="uq_usage_counter_window",
        ),
    )
    op.create_index(op.f("ix_usage_counters_id"), "usage_counters", ["id"], unique=False)

    op.create_table(
        "usage_events",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("policy_id", sa.String(36), nullable=True),
        sa.Column("metric", sa.String(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("scope_type", sa.String(), nullable=False),
        sa.Column("scope_ref_id", sa.String(36), nullable=False),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("source_ref_id", sa.String(36), nullable=True),
        sa.Column("usage_metadata", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["policy_id"], ["usage_policies.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id"),
    )
    op.create_index(op.f("ix_usage_events_id"), "usage_events", ["id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_usage_events_id"), table_name="usage_events")
    op.drop_table("usage_events")
    op.drop_index(op.f("ix_usage_counters_id"), table_name="usage_counters")
    op.drop_table("usage_counters")
    op.drop_index(op.f("ix_usage_policy_connection_overrides_id"), table_name="usage_policy_connection_overrides")
    op.drop_table("usage_policy_connection_overrides")
    op.drop_index(op.f("ix_usage_policy_assignments_id"), table_name="usage_policy_assignments")
    op.drop_table("usage_policy_assignments")
    op.drop_index(op.f("ix_usage_policies_id"), table_name="usage_policies")
    op.drop_table("usage_policies")

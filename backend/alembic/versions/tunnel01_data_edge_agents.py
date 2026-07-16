"""data edge agents + connection tunnel fields (design D2/D3)

Revision ID: tunnel01
Revises: durctx01
Create Date: 2026-09-02

Creates data_edge_agents (one row per agent per org, upserted by the
advertisement handler) and adds tunnel_mode / edge_agent_id to connections.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "tunnel01"
down_revision: Union[str, None] = "durctx01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "data_edge_agents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("edge_agent_id", sa.String(length=255), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=True),
        sa.Column("last_connected_at", sa.DateTime(), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("client_version", sa.String(length=50), nullable=True),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("last_advertisement", sa.JSON(), nullable=True),
        sa.Column("last_advertised_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "edge_agent_id", name="uq_data_edge_agents_org_agent"),
    )
    op.create_index(op.f("ix_data_edge_agents_id"), "data_edge_agents", ["id"], unique=False)
    op.create_index(op.f("ix_data_edge_agents_organization_id"), "data_edge_agents", ["organization_id"], unique=False)

    op.add_column("connections", sa.Column("tunnel_mode", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("connections", sa.Column("edge_agent_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("connections", "edge_agent_id")
    op.drop_column("connections", "tunnel_mode")
    op.drop_index(op.f("ix_data_edge_agents_organization_id"), table_name="data_edge_agents")
    op.drop_index(op.f("ix_data_edge_agents_id"), table_name="data_edge_agents")
    op.drop_table("data_edge_agents")

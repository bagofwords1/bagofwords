"""data_sources.kind: agent vs connector

Revision ID: dskind01
Revises: rel0611
Create Date: 2026-11-07 00:00:00.000000

Nullable: NULL means "derive from the connection types" (see
app/services/data_source_kind.py), so existing rows keep working unchanged.

Backfill: an MCP-backed data source that an admin curated with instructions is
an analysis agent people already pick on purpose — keep it an agent rather
than letting the derived default turn it into a connector.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'dskind01'
down_revision: Union[str, None] = 'rel0611'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("data_sources")}
    if "kind" not in cols:
        with op.batch_alter_table("data_sources") as batch:
            batch.add_column(sa.Column("kind", sa.String(length=16), nullable=True))
    op.execute(
        "UPDATE data_sources SET kind = 'agent' "
        "WHERE kind IS NULL "
        "AND id IN (SELECT data_source_id FROM instruction_data_source_association) "
        "AND id IN (SELECT dc.data_source_id FROM domain_connection dc "
        "           JOIN connections c ON c.id = dc.connection_id WHERE c.type = 'mcp')"
    )


def downgrade() -> None:
    # Idempotent both ways: the dashboard test corpus replays this unstamped.
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("data_sources")}
    if "kind" in cols:
        with op.batch_alter_table("data_sources") as batch:
            batch.drop_column("kind")

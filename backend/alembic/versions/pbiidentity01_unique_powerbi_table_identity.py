"""Repair duplicate Power BI identities without dropping selections or grants.

Revision ID: pbiidentity01
Revises: cachettl01
"""
from alembic import op
import sqlalchemy as sa
from app.core.migrations.powerbi_identity_v1 import repair_powerbi_identities

revision = "pbiidentity01"
down_revision = "cachettl01"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    # Migration runs before app workers start. Repair references before enforcing
    # uniqueness. NULL/malformed metadata and BOW-managed tables are unaffected.
    connections = sa.table("connections", sa.column("id", sa.String), sa.column("type", sa.String))
    for connection_id in bind.execute(sa.select(connections.c.id).where(connections.c.type == "powerbi")).scalars():
        repair_powerbi_identities(bind, connection_id)
    if bind.dialect.name == "postgresql":
        workspace = "metadata_json->'powerbi'->>'workspaceId'"
        dataset = "metadata_json->'powerbi'->>'datasetId'"
        table = "metadata_json->'powerbi'->>'tableName'"
    else:
        workspace = "json_extract(metadata_json, '$.powerbi.workspaceId')"
        dataset = "json_extract(metadata_json, '$.powerbi.datasetId')"
        table = "json_extract(metadata_json, '$.powerbi.tableName')"
    op.execute(sa.text(f"""CREATE UNIQUE INDEX uq_connection_powerbi_identity
        ON connection_tables (connection_id, (COALESCE({workspace}, '')), ({dataset}), ({table}))
        WHERE kind = 'table' AND {dataset} IS NOT NULL AND {dataset} <> ''
          AND {table} IS NOT NULL AND {table} <> ''"""))


def downgrade():
    op.drop_index("uq_connection_powerbi_identity", table_name="connection_tables")
    # Consolidated rows are deliberately not recreated on downgrade.

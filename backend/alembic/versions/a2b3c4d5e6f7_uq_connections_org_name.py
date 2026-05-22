"""add unique constraint on connections(organization_id, name)

Revision ID: a2b3c4d5e6f7
Revises: z1a2b3c4d5e6
Create Date: 2026-05-22 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c5d6e7f8g9h0'
down_revision: Union[str, None] = 'e8f9a0b1c2d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # Find duplicate (organization_id, name) pairs; keep the earliest, rename the rest
    duplicates = conn.execute(sa.text("""
        SELECT id, organization_id, name
        FROM connections
        WHERE deleted_at IS NULL
          AND (organization_id, name) IN (
              SELECT organization_id, name
              FROM connections
              WHERE deleted_at IS NULL
              GROUP BY organization_id, name
              HAVING COUNT(*) > 1
          )
        ORDER BY organization_id, name, created_at
    """)).fetchall()

    seen = {}
    for row in duplicates:
        key = (row.organization_id, row.name)
        if key not in seen:
            seen[key] = True
        else:
            new_name = f"{row.name}_{row.id[:8]}"
            conn.execute(sa.text(
                "UPDATE connections SET name = :new_name WHERE id = :id"
            ), {"new_name": new_name, "id": row.id})

    with op.batch_alter_table('connections') as batch_op:
        batch_op.create_unique_constraint('uq_connections_org_name', ['organization_id', 'name'])


def downgrade() -> None:
    with op.batch_alter_table('connections') as batch_op:
        batch_op.drop_constraint('uq_connections_org_name', type_='unique')

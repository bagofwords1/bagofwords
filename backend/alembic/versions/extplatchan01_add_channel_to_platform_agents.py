"""add external_channel_id to external_platform_agents (channel-level scoping)

Revision ID: extplatchan01
Revises: extplatagent01
Create Date: 2026-11-10 00:00:00.000000

Generalizes the per-instance agent scope (one WhatsApp number -> a set of
agents) to also carry a *channel* dimension, so a single Slack workspace /
Teams tenant — which is one platform instance — can point an individual channel
at specific agents.

``external_channel_id`` defaults to ``""`` (empty). An empty value is the
connection default: it keeps the exact pre-existing per-instance meaning, so
every existing WhatsApp scope row is unchanged. A non-empty value scopes one
channel (Slack ``event.channel`` / Teams ``channelData.channel.id``).

The unique constraint moves from ``(platform_id, data_source_id)`` to
``(platform_id, external_channel_id, data_source_id)``. A sentinel ``""`` rather
than NULL is used so the constraint behaves identically on SQLite and Postgres
(both treat NULLs as distinct, which would permit duplicate default rows).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'extplatchan01'
down_revision: Union[str, None] = 'extplatagent01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add the column with a server default so existing rows backfill to "" (the
    # connection default / pre-existing per-instance meaning).
    op.add_column(
        'external_platform_agents',
        sa.Column(
            'external_channel_id',
            sa.String(length=255),
            nullable=False,
            server_default='',
        ),
    )
    op.create_index(
        'ix_external_platform_agents_external_channel_id',
        'external_platform_agents',
        ['external_channel_id'],
    )
    # Swap the unique constraint to include the channel dimension. Use a
    # batch op so SQLite (which cannot ALTER constraints in place) rebuilds the
    # table; Postgres runs the drop/create directly.
    with op.batch_alter_table('external_platform_agents', schema=None) as batch_op:
        batch_op.drop_constraint('uq_external_platform_agent', type_='unique')
        batch_op.create_unique_constraint(
            'uq_external_platform_agent',
            ['platform_id', 'external_channel_id', 'data_source_id'],
        )


def downgrade() -> None:
    with op.batch_alter_table('external_platform_agents', schema=None) as batch_op:
        batch_op.drop_constraint('uq_external_platform_agent', type_='unique')
        batch_op.create_unique_constraint(
            'uq_external_platform_agent',
            ['platform_id', 'data_source_id'],
        )
    op.drop_index(
        'ix_external_platform_agents_external_channel_id',
        table_name='external_platform_agents',
    )
    op.drop_column('external_platform_agents', 'external_channel_id')

"""Give a test suite an optional home agent (per-agent Drafts bucket)

Revision ID: evsuite0001
Revises: dpanl0001
Create Date: 2026-08-07

The default "Drafts" suite is per-organization, so in an org with many agents it
becomes one shared pile holding every agent's auto-drafted cases. It is also
mixed by construction, which made it unrunnable: running a suite requires
authority over every case in it, and a shared bucket always contains someone
else's.

``data_source_id`` is a HOME, not a scope — it says where new cases land by
default and lets an EMPTY per-agent Drafts be found at all (with no cases, there
is nothing to derive the agent from). It does not constrain what a case in the
suite may target, and suite authority still derives from the cases held. NULL is
the org-wide suite, which is what every existing row becomes.
"""
from alembic import op
import sqlalchemy as sa


revision = 'evsuite0001'
down_revision = 'dpanl0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('test_suites') as batch_op:
        batch_op.add_column(sa.Column('data_source_id', sa.String(36), nullable=True))
        batch_op.create_index(
            'ix_test_suites_data_source_id', ['data_source_id'], unique=False
        )
        batch_op.create_foreign_key(
            'fk_test_suites_data_source_id', 'data_sources', ['data_source_id'], ['id']
        )


def downgrade() -> None:
    with op.batch_alter_table('test_suites') as batch_op:
        batch_op.drop_constraint('fk_test_suites_data_source_id', type_='foreignkey')
        batch_op.drop_index('ix_test_suites_data_source_id')
        batch_op.drop_column('data_source_id')

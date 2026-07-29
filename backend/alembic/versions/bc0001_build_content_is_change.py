"""mark real changes on build_contents (is_change)

Revision ID: bc0001chg
Revises: proj0002
Create Date: 2026-07-28 00:00:00.000000

A build snapshots EVERY instruction, so build_contents grows as
(builds × instructions) while only a handful of rows per build are an actual
change. The pending-review sweep had to discover those rows at read time with
an anti-join against the base build's snapshot — scanning the org's entire
draft-build corpus on every /agents page load.

This records the answer at write time. `is_change` is true exactly when the
row differs from the build's base build (different version, absent from base,
or no base build at all) — the same predicate the anti-join computed.

The backfill runs that anti-join once, here, so existing rows carry the flag. It
is a single set-based UPDATE over `build_contents`, which is the largest table in
an instruction-heavy org — expect it to take the time one full pass costs there.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bc0001chg'
down_revision: Union[str, None] = 'proj0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Same predicate as the removed anti-join: a row is a change unless the build's
# base build holds this instruction at this exact version. `uq_build_content_
# build_instruction` (build_id, instruction_id) makes the correlated lookup an
# index probe, so this is a one-time cost proportional to the table size.
_BACKFILL = """
UPDATE build_contents SET is_change = {true_}
WHERE id IN (
    SELECT bc.id
    FROM build_contents bc
    JOIN instruction_builds b ON b.id = bc.build_id
    WHERE b.base_build_id IS NULL
       OR NOT EXISTS (
            SELECT 1 FROM build_contents base
            WHERE base.build_id = b.base_build_id
              AND base.instruction_id = bc.instruction_id
              AND base.instruction_version_id = bc.instruction_version_id
       )
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'
    server_default = sa.text('false') if is_pg else sa.text('0')

    op.add_column(
        'build_contents',
        sa.Column('is_change', sa.Boolean(), nullable=False, server_default=server_default),
    )
    # Backfill before indexing: one pass over the table, then build the index
    # once, instead of maintaining it through every updated row.
    op.execute(sa.text(_BACKFILL.format(true_='true' if is_pg else '1')))
    op.create_index(
        'ix_build_contents_is_change_build', 'build_contents', ['is_change', 'build_id']
    )


def downgrade() -> None:
    op.drop_index('ix_build_contents_is_change_build', table_name='build_contents')
    op.drop_column('build_contents', 'is_change')

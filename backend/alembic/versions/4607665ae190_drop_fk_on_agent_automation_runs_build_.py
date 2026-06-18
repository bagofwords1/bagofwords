"""drop FK on agent_automation_runs.build_id (soft/transient build ids)

Revision ID: 4607665ae190
Revises: 52735eef8bf7
Create Date: 2026-06-15 13:38:50.314567

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4607665ae190'
down_revision: Union[str, None] = '52735eef8bf7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # agent_automation_runs.build_id holds transient/stub build ids that may not
    # exist in instruction_builds, so the hard FK created by the table migration
    # causes ForeignKeyViolationError on Postgres. Drop it (soft reference).
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE agent_automation_runs "
            "DROP CONSTRAINT IF EXISTS agent_automation_runs_build_id_fkey"
        )
    # SQLite: foreign keys aren't enforced and the inline constraint is unnamed;
    # nothing to drop.


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.create_foreign_key(
            "agent_automation_runs_build_id_fkey",
            "agent_automation_runs",
            "instruction_builds",
            ["build_id"],
            ["id"],
        )

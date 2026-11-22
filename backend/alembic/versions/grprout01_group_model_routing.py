"""per-group Auto model router switch

Revision ID: grprout01
Revises: rel0631merge
Create Date: 2026-11-22 00:00:00.000000

Adds ``groups.model_routing``, a nullable boolean that turns the Auto model
router on or off for a group's members. NULL (every existing row) means
"follow the org ``model_routing`` setting", so behavior is unchanged until an
admin sets a group. A member in several groups gets the value of the
highest-priority group that has one (same ``default_llm_priority`` order as
the group default models).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'grprout01'
down_revision: Union[str, None] = 'rel0631merge'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("groups")}
    if "model_routing" not in cols:
        op.add_column("groups", sa.Column("model_routing", sa.Boolean(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("groups")}
    if "model_routing" in cols:
        op.drop_column("groups", "model_routing")

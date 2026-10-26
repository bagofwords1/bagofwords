"""group-level default LLM models

Revision ID: grpllm01
Revises: submsg02
Create Date: 2026-10-26 00:00:00.000000

Adds per-group default and small-default models plus a priority that decides
which group's default wins for a user in several groups (lower wins, ties by
group name). Both model columns are soft references to llm_models.id — no
DB-level FK, same convention as memberships.default_llm_model_id and
reports.model_id: stale values are skipped leniently at read time.

Also lifts the old "org defaults are always open" rule: from now on a
restricted org default is restricted. To keep every existing org's effective
access unchanged, any model that is currently BOTH restricted and an org
default/small default (which the old rule treated as open to everyone) is
un-restricted here.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'grpllm01'
down_revision: Union[str, None] = 'submsg02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("groups")}
    if "default_llm_model_id" not in cols:
        op.add_column("groups", sa.Column("default_llm_model_id", sa.String(36), nullable=True))
    if "small_default_llm_model_id" not in cols:
        op.add_column("groups", sa.Column("small_default_llm_model_id", sa.String(36), nullable=True))
    if "default_llm_priority" not in cols:
        op.add_column(
            "groups",
            sa.Column("default_llm_priority", sa.Integer(), nullable=False, server_default="100"),
        )

    # Preserve today's behavior: restricted defaults were open to everyone.
    op.execute(
        sa.text(
            "UPDATE llm_models SET is_restricted = :f "
            "WHERE is_restricted = :t AND (is_default = :t OR is_small_default = :t)"
        ).bindparams(t=True, f=False)
    )


def downgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("groups")}
    for name in ("default_llm_priority", "small_default_llm_model_id", "default_llm_model_id"):
        if name in cols:
            op.drop_column("groups", name)

"""build_contents.scope_change — a build's proposed change of an instruction's agents

Revision ID: scopechg01
Revises: grpllm01
Create Date: 2026-10-27 00:00:00.000000

Adds the column only. NULL on every existing row: a scope change is written
solely by the AI edit_instruction tool from this release on, so no row that
exists today proposes one, and no existing data is read or rewritten.

The column sits on the build row rather than the version (see
BuildContent.scope_change): a proposal must live and die with the build that
made it, never travel into main or into copies of other builds. An earlier
draft of this revision put it on instruction_versions; that column is dropped
here if present (it never shipped).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'scopechg01'
down_revision: Union[str, None] = 'grpllm01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "scope_change" not in {c["name"] for c in insp.get_columns("build_contents")}:
        op.add_column("build_contents", sa.Column("scope_change", sa.JSON(), nullable=True))
    if "scope_change" in {c["name"] for c in insp.get_columns("instruction_versions")}:
        with op.batch_alter_table("instruction_versions") as batch:
            batch.drop_column("scope_change")


def downgrade() -> None:
    bind = op.get_bind()
    if "scope_change" in {c["name"] for c in sa.inspect(bind).get_columns("build_contents")}:
        with op.batch_alter_table("build_contents") as batch:
            batch.drop_column("scope_change")

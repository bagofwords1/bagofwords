"""add default_data_source_ids to memberships

Revision ID: usrdsdef01
Revises: oidcgrp01
Create Date: 2026-08-08 00:00:00.000000

Per-user default agent scope, scoped per organization (memberships is the
per-user-per-org record, same as the custom-instructions note and the default
LLM model). JSON list of data_source ids — soft references with no DB-level
FK on purpose: a stale id (agent deleted, or access revoked later) is pruned
at read time, so deletes never need to cascade here.

NULL is the default and means Auto (no pin), which is also how a report
encodes it. Nothing is backfilled: every existing member starts on Auto.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'usrdsdef01'
down_revision: Union[str, None] = 'oidcgrp01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent add: dev databases may already carry the column from an
    # earlier run of this migration while alembic_version points before it.
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("memberships")}
    if "default_data_source_ids" not in cols:
        op.add_column("memberships", sa.Column("default_data_source_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("memberships")}
    if "default_data_source_ids" in cols:
        op.drop_column("memberships", "default_data_source_ids")

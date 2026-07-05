"""add default_llm_model_id to memberships

Revision ID: ud1a2b3c4d5e
Revises: rbacbf01
Create Date: 2026-07-05 00:00:00.000000

Per-user default LLM model, scoped per organization (memberships is the
per-user-per-org record, same as the custom-instructions note). Soft
reference to llm_models.id — no DB-level FK on purpose: a stale value
(model disabled/restricted/deleted later) is resolved at read time by
falling back to the organization default, so deletes never need to
cascade here.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'ud1a2b3c4d5e'
down_revision: Union[str, None] = 'rbacbf01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("memberships", sa.Column("default_llm_model_id", sa.String(36), nullable=True))


def downgrade() -> None:
    op.drop_column("memberships", "default_llm_model_id")

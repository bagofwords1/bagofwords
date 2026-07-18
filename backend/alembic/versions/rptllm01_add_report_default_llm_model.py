"""add model_id (report-level LLM override) to reports

Revision ID: rptllm01
Revises: v1s2v3o4t5g6
Create Date: 2026-07-16 00:00:00.000000

Report-level LLM override, soft reference to llm_models.id — no DB-level FK on
purpose, same convention as memberships.default_llm_model_id and
prompt.model_id. A stale value (model disabled/restricted/deleted later, or set
by a teammate the current user cannot use) is resolved leniently at read time
by falling back to the user default, then the organization default, so deletes
never need to cascade here. Precedence at completion time:
prompt.model_id > report.model_id > user default > org default.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'rptllm01'
down_revision: Union[str, None] = 'v1s2v3o4t5g6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent add: dev databases may already carry the column from an
    # earlier run of this migration while alembic_version points before it.
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("reports")}
    if "model_id" not in cols:
        op.add_column("reports", sa.Column("model_id", sa.String(36), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("reports")}
    if "model_id" in cols:
        op.drop_column("reports", "model_id")

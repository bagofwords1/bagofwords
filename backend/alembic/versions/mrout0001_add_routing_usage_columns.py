"""add routing columns to llm_usage_records (+ merge report-model and umbrella heads)

Revision ID: mrout0001
Revises: rptllm01, umbr0001
Create Date: 2026-07-18 00:00:00.000000

Auto model routing: llm_usage_records gains `routed` (bool) and
`baseline_model_id` (soft ref to llm_models.id) so the cost console can compute
realized savings — baseline-priced tokens minus actual cost — over calls made
during a routed run. Older rows stay routed=False / NULL.

Also a merge point: the report-level LLM override (rptllm01) and the umbrella
head (umbr0001) were two open heads on this branch; this unifies them so
`alembic upgrade head` resolves to a single revision.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'mrout0001'
down_revision: Union[str, Sequence[str], None] = ('rptllm01', 'umbr0001')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("llm_usage_records")}
    if "routed" not in cols:
        op.add_column(
            "llm_usage_records",
            sa.Column("routed", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if "baseline_model_id" not in cols:
        op.add_column(
            "llm_usage_records",
            sa.Column("baseline_model_id", sa.String(), nullable=True),
        )
    # Index for the routed filter used by the savings aggregation.
    existing_idx = {i["name"] for i in sa.inspect(bind).get_indexes("llm_usage_records")}
    if "ix_llm_usage_records_routed" not in existing_idx:
        op.create_index("ix_llm_usage_records_routed", "llm_usage_records", ["routed"])


def downgrade() -> None:
    bind = op.get_bind()
    existing_idx = {i["name"] for i in sa.inspect(bind).get_indexes("llm_usage_records")}
    if "ix_llm_usage_records_routed" in existing_idx:
        op.drop_index("ix_llm_usage_records_routed", table_name="llm_usage_records")
    cols = {c["name"] for c in sa.inspect(bind).get_columns("llm_usage_records")}
    if "baseline_model_id" in cols:
        op.drop_column("llm_usage_records", "baseline_model_id")
    if "routed" in cols:
        op.drop_column("llm_usage_records", "routed")

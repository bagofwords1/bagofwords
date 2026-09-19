"""split cache writes by TTL and track reasoning tokens on llm_usage_records

Anthropic bills a 1-hour cache write at 2x the input rate and a 5-minute one at
1.25x, and reports the split back on every response. Without somewhere to put
it, both prices collapse onto one token count and the cost console understates
spend wherever the longer TTL is in use. ``reasoning_tokens`` is a subset of
``completion_tokens`` that OpenAI-family responses report; it bills at the
output rate and was previously indistinguishable from ordinary output.

Both default to 0, so existing rows keep their current (5-minute, no-reasoning)
interpretation, which is what they were actually billed at.

Revision ID: cachettl01
Revises: idxactivity01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'cachettl01'
down_revision: Union[str, None] = 'idxactivity01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = ("cache_write_1h_tokens", "reasoning_tokens")


def _existing(bind) -> set:
    return {c["name"] for c in sa.inspect(bind).get_columns("llm_usage_records")}


def upgrade() -> None:
    bind = op.get_bind()
    have = _existing(bind)
    for name in _COLUMNS:
        if name in have:
            continue
        # server_default backfills existing rows without a table rewrite; the
        # model-side default keeps new inserts explicit.
        op.add_column(
            "llm_usage_records",
            sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    have = _existing(bind)
    for name in reversed(_COLUMNS):
        if name in have:
            op.drop_column("llm_usage_records", name)

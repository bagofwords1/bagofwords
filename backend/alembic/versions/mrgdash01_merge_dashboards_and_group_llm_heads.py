"""merge the dashboards (refhist01) and per-group LLM defaults (grpllm01) heads

Revision ID: mrgdash01
Revises: refhist01, grpllm01
Create Date: 2026-10-27 09:00:00.000000

"""
from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = 'mrgdash01'
down_revision: Union[str, None] = ('refhist01', 'grpllm01')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

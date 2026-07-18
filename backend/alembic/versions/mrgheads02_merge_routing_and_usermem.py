"""merge auto-routing and per-user-memory heads

Revision ID: mrgheads02
Revises: mrout0001, usermem01
Create Date: 2026-07-18 00:00:00.000000

No schema change — unifies the two migration heads that resulted from the
auto-model-routing branch (mrout0001) and the per-user agent memory feature
(usermem01) landing in parallel, so `alembic upgrade head` resolves to one
revision.
"""
from typing import Sequence, Union

revision: str = "mrgheads02"
down_revision: Union[str, Sequence[str], None] = ("mrout0001", "usermem01")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

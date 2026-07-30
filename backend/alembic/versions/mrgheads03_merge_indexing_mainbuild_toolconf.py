"""merge connection-indexing-user-scope, one-main-build and tool-confirmations heads

Revision ID: mrgheads03
Revises: idxuser01, mainbuild01, toolconf01
Create Date: 2026-07-26 13:10:00.000000

No schema change — three features branched off entraprof01 and landed in
parallel (per-user connection indexing scope, the one-main-build-per-org
constraint, and the tool_confirmations table), leaving three heads so
`alembic upgrade head` failed with MultipleHeads and every test that builds a
schema errored. This unifies them; the three can be applied in any order.
"""
from typing import Sequence, Union

revision: str = "mrgheads03"
down_revision: Union[str, Sequence[str], None] = ("idxuser01", "mainbuild01", "toolconf01")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

"""query_executions.killable for databases that ran an earlier queryexec01

`queryexec01` creates `killable` today, but an earlier build of it (deployed
from the branch image) did not, and a database stamped with that revision
never re-runs it. Add the column where it is missing, so tracked queries do
not fail on it. A no-op everywhere else.

Revision ID: queryexec02
Revises: queryexec01
Create Date: 2026-11-16
"""
import sqlalchemy as sa

from alembic import op

revision = "queryexec02"
down_revision = "queryexec01"
branch_labels = None
depends_on = None


def _has_killable() -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("query_executions"):
        return True  # nothing to repair
    return "killable" in {c["name"] for c in inspector.get_columns("query_executions")}


def upgrade() -> None:
    # Idempotent: the dashboard test corpus applies this unstamped.
    if _has_killable():
        return
    op.add_column(
        "query_executions",
        sa.Column("killable", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    # queryexec01 owns the column on every database it created; dropping it
    # here would leave those inconsistent. queryexec01's downgrade drops the
    # whole table.
    pass

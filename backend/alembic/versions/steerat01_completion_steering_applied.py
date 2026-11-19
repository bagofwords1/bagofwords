"""add completions.steering_applied_at / steering_loop_index

A steering message ("Send now" while the agent runs) is picked up by the agent
at a specific step of its loop. Record when, and before which loop step, so the
chat can render the steer right above the step that acted on it instead of
guessing from send time (which put it at the bottom of the turn).

Both nullable: steers the agent never picked up, and rows written before this
revision, keep the send-time placement.

Revision ID: steerat01
Revises: worldai01
Create Date: 2026-11-18
"""
from alembic import op
import sqlalchemy as sa


revision = "steerat01"
down_revision = "worldai01"
branch_labels = None
depends_on = None


TABLE = "completions"
COLUMNS = (
    ("steering_applied_at", sa.DateTime()),
    ("steering_loop_index", sa.Integer()),
)


def _columns() -> set:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    # Idempotent: the dashboard test corpus applies this unstamped, so a later
    # `upgrade head` runs it again.
    missing = [(name, type_) for name, type_ in COLUMNS if name not in _columns()]
    if not missing:
        return
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        for name, type_ in missing:
            batch_op.add_column(sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    present = [name for name, _ in COLUMNS if name in _columns()]
    if not present:
        return
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        for name in present:
            batch_op.drop_column(name)

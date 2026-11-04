"""Persist remote command rules per user and tool."""

import sqlalchemy as sa

from alembic import op

revision = "remotepolicy01"
down_revision = "memberdup01"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user_connection_tool_preferences", sa.Column("command_rules", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("user_connection_tool_preferences", "command_rules")

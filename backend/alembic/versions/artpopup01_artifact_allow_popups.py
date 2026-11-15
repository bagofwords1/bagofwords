"""add allow_popups to artifacts

Per-artifact opt-in that relaxes the artifact iframe's sandbox so links the
app renders may open a new tab (allow-popups + allow-popups-to-escape-sandbox)
and the app may use alert/confirm/prompt (allow-modals). Off by default — the
tight sandbox is deliberate for LLM-generated code. Set from the dashboard's
Embed tab; applies wherever the artifact renders (editor, /d, /r).

Revision ID: artpopup01
Revises: fileindex01
Create Date: 2026-11-12
"""
from alembic import op
import sqlalchemy as sa


revision = "artpopup01"
down_revision = "fileindex01"
branch_labels = None
depends_on = None


def _has_column() -> bool:
    return any(c["name"] == "allow_popups" for c in sa.inspect(op.get_bind()).get_columns("artifacts"))


def upgrade() -> None:
    # Idempotent: the dashboard test corpus applies this unstamped.
    if _has_column():
        return
    op.add_column(
        "artifacts",
        sa.Column("allow_popups", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    if not _has_column():
        return
    op.drop_column("artifacts", "allow_popups")

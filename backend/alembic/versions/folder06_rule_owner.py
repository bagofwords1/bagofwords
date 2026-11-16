"""Keep the owning folder independent of an instruction's reusable links."""

import sqlalchemy as sa
from alembic import op

revision = "folder06"
down_revision = "folder05"
branch_labels = None
depends_on = None


def _applied():
    """Idempotent: the dashboard test corpus replays this revision unstamped
    (tests/fixtures/dashboard_corpus/alembic_at.py), then `upgrade head` runs
    it again."""
    return "owner_folder_id" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("instructions")}


def upgrade():
    if _applied():
        return
    with op.batch_alter_table("instructions") as batch:
        batch.add_column(sa.Column("owner_folder_id", sa.String(36)))
        batch.create_foreign_key("fk_instruction_owner_folder", "folders", ["owner_folder_id"], ["id"])


def downgrade():
    if not _applied():
        return
    with op.batch_alter_table("instructions") as batch:
        batch.drop_constraint("fk_instruction_owner_folder", type_="foreignkey")
        batch.drop_column("owner_folder_id")

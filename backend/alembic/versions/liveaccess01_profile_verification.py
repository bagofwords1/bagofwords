"""Track the directory observation used for live reader authorization."""
from alembic import op
import sqlalchemy as sa

revision = "liveaccess01"
down_revision = "fabhold01"
branch_labels = None
depends_on = None


def upgrade():
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("query_executions")}
    for name, dtype in (("policy_version", sa.String(64)), ("protected_statement", sa.Text())):
        if name not in columns:
            op.add_column("query_executions", sa.Column(name, dtype, nullable=True))
    if "profile_verified_at" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("memberships")}:
        op.add_column("memberships", sa.Column("profile_verified_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("query_executions", "protected_statement")
    op.drop_column("query_executions", "policy_version")
    op.drop_column("memberships", "profile_verified_at")

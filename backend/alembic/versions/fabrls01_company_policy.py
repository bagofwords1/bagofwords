"""Store Fabric company policies independently of editable connector config."""
from alembic import op
import sqlalchemy as sa

revision = "fabrls01"
down_revision = "mrgheads12"
branch_labels = None
depends_on = None


def upgrade():
    existing = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("connections")}
    for name in ("company_policy", "active_company_policy"):
        if name not in existing:
            op.add_column("connections", sa.Column(name, sa.JSON(none_as_null=True), nullable=True))


def downgrade():
    op.drop_column("connections", "active_company_policy")
    op.drop_column("connections", "company_policy")

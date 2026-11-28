"""Merge accelerated-table CLS and live-connection access policy migrations."""
revision = "accessmerge01"
down_revision = ("liveaccess01", "clscol01")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass

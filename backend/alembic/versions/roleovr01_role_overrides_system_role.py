"""add overrides_system_role to roles

Enterprise orgs can customize the built-in `member` role. On first edit we
materialize a per-org, editable (`is_system=False`) copy of the system role and
re-point that org's member assignments to it. `overrides_system_role` holds the
base system-role name the copy overrides (currently only "member"); it is NULL
for the global system rows and for ordinary custom roles. Resolution of which
role an assignment targets keys off this column (see
permission_resolver.resolve_assignable_system_role).

Revision ID: roleovr01
Revises: extplatagent01
Create Date: 2026-11-10
"""
from alembic import op
import sqlalchemy as sa


revision = "roleovr01"
down_revision = "extplatagent01"
branch_labels = None
depends_on = None


def _has_column() -> bool:
    return any(c["name"] == "overrides_system_role" for c in sa.inspect(op.get_bind()).get_columns("roles"))


def upgrade() -> None:
    # Idempotent: the dashboard test corpus applies this unstamped.
    if _has_column():
        return
    op.add_column(
        "roles",
        sa.Column("overrides_system_role", sa.String(length=50), nullable=True),
    )


def downgrade() -> None:
    if not _has_column():
        return
    op.drop_column("roles", "overrides_system_role")

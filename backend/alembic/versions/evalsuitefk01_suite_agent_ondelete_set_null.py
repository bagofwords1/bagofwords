"""Set suite home agent FK to ON DELETE SET NULL.

Revision ID: evalsuitefk01
Revises: oauthapp01
Create Date: 2026-08-17
"""

from collections.abc import Sequence

from alembic import op


revision: str = "evalsuitefk01"
down_revision: str | None = "oauthapp01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("test_suites") as batch_op:
        batch_op.drop_constraint("fk_test_suites_data_source_id", type_="foreignkey")
        batch_op.create_foreign_key(
            "fk_test_suites_data_source_id",
            "data_sources",
            ["data_source_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("test_suites") as batch_op:
        batch_op.drop_constraint("fk_test_suites_data_source_id", type_="foreignkey")
        batch_op.create_foreign_key(
            "fk_test_suites_data_source_id",
            "data_sources",
            ["data_source_id"],
            ["id"],
        )

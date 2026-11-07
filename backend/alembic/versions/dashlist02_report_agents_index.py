"""Dashboards list: an index for the agent filter

Revision ID: dashlist02
Revises: dashlist01
Create Date: 2026-11-05

- `ix_report_data_source_association_report_ds` on
  `report_data_source_association(report_id, data_source_id)`. The
  dashboards list's agent filter and its agents facet look up the agents of
  a draft dashboard's conversation by `report_id`; the table had no index
  (no key either), so each lookup read the whole table. Non-unique: a
  conversation can link an agent twice.

Additive. Idempotent both ways, as dashlist01: the dashboard corpus applies
the upgrade unstamped and undoes it unstamped, so each step checks first.
The downgrade drops the index natively (never batch_alter_table: on SQLite
a batch rebuild of a table with foreign keys on can cascade into children).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "dashlist02"
down_revision: str | None = "dashlist01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "report_data_source_association"
INDEX = "ix_report_data_source_association_report_ds"


def _has_index() -> bool:
    return INDEX in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(TABLE)}


def upgrade() -> None:
    if not _has_index():
        op.create_index(INDEX, TABLE, ["report_id", "data_source_id"])


def downgrade() -> None:
    if _has_index():
        op.drop_index(INDEX, table_name=TABLE)

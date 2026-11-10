"""Viewer results: stamp what each run read

Revision ID: vrstamp01
Revises: queryexec03
Create Date: 2026-11-10

`step_user_results.source_stamp` (String 64) and `source_refs` (Text). A
viewer's stored result is served back to them only while what its run read
still holds: the identity mode, the auth policy of the connections behind its
sources, the extract and row policy of each accelerated relation, the row
filter compiled for that viewer, and their identity when the step is
identity-scoped (app/services/viewer_result_freshness.py). `source_stamp`
hashes all of it; `source_refs` names the connection and relation versions
so a change to one finds the rows it outdates.

Additive and nullable: rows stored before this revision have no stamp, and
the read decides them (a legacy row is served only where nothing the stamp
tracks is in play). Idempotent both ways, as pubkeep01: the dashboard corpus
applies upgrades unstamped and undoes them unstamped.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "vrstamp01"
down_revision: str | None = "queryexec03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "step_user_results"


def _columns() -> set:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    have = _columns()
    with op.batch_alter_table(TABLE) as batch:
        if "source_stamp" not in have:
            batch.add_column(sa.Column("source_stamp", sa.String(64), nullable=True))
        if "source_refs" not in have:
            batch.add_column(sa.Column("source_refs", sa.Text(), nullable=True))


def downgrade() -> None:
    have = _columns()
    with op.batch_alter_table(TABLE) as batch:
        if "source_refs" in have:
            batch.drop_column("source_refs")
        if "source_stamp" in have:
            batch.drop_column("source_stamp")

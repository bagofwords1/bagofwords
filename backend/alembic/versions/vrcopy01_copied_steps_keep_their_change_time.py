"""Viewer results: a step the upgrade copied keeps the time it last changed

Revision ID: vrcopy01
Revises: vrstamp01
Create Date: 2026-11-21

dashfc08 published every shared conversation as a release: it copied the
conversation's steps and each reader's result of them (decision 9: readers
keep the result they had), stamping each copy updated_at = the time of the
upgrade. A reader's stored result is served only if its run is not older
than its step's (and its loaded upstreams') updated_at
(app/services/viewer_result_freshness.py), so that stamp made every copied
result older than its step: every reader lost the result they had.

Copying a step does not change what it holds. Each copy dashfc08 made that
has not changed since (updated_at still equals created_at, both the upgrade's
time) gets back the updated_at of the step it was copied from -- the step of
the same conversation with the same title and the same data (copied as it
is, byte for byte, encrypted or not). A copied result is then judged as the
conversation judged it before the upgrade: served if it ran after its step
last changed, refused if not. A copy refreshed since the upgrade keeps its
time, and with it refuses the results it outdated. A copy whose source no
longer matches (changed or gone since) is left as it is: its results stay
refused. Where more than one step matches, the latest of their times is
kept: never earlier than the true source's.

Only rows dashfc08 recorded as its own inserts (dashfc08_undo) are read,
with no application code. Idempotent: a copy given its source's time no
longer has updated_at == created_at. Before dashfc08 has run (the dashboard
corpus applies later revisions unstamped) there is nothing to do. The
downgrade leaves the times as they are: they are the copies' true times, and
dashfc08's own downgrade deletes the copies.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "vrcopy01"
down_revision: str | None = "vrstamp01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UNDO = "dashfc08_undo"
TABLES = (UNDO, "steps", "queries", "artifact_releases", "artifacts")
CHUNK = 500


def upgrade() -> None:
    conn = op.get_bind()
    if not set(TABLES) <= set(sa.inspect(conn).get_table_names()):
        return
    meta = sa.MetaData()
    undo, steps, queries, releases, artifacts = (sa.Table(n, meta, autoload_with=conn) for n in TABLES)
    copied = [str(r[0]) for r in conn.execute(
        sa.select(undo.c.row_id).where(undo.c.table_name == "steps", undo.c.action == "insert")).all()]

    cp, cq = steps.alias("cp"), queries.alias("cq")
    src, sq = steps.alias("src"), queries.alias("sq")
    for i in range(0, len(copied), CHUNK):
        sources = conn.execute(
            sa.select(cp.c.id, cp.c.updated_at, sa.func.max(src.c.updated_at).label("changed_at"))
            .select_from(
                cp.join(cq, cq.c.id == cp.c.query_id)
                .join(releases, releases.c.id == cq.c.artifact_release_id)
                .join(artifacts, artifacts.c.id == releases.c.artifact_id)
                .join(sq, sq.c.report_id == artifacts.c.report_id)
                .join(src, src.c.query_id == sq.c.id))
            .where(cp.c.id.in_(copied[i:i + CHUNK]),
                   cp.c.updated_at == cp.c.created_at,
                   src.c.id != cp.c.id,
                   src.c.title == cp.c.title,
                   sa.cast(src.c.data, sa.Text) == sa.cast(cp.c.data, sa.Text))
            .group_by(cp.c.id, cp.c.updated_at)
        ).all()
        for copy_id, copied_at, changed_at in sources:
            if changed_at is not None and changed_at < copied_at:
                conn.execute(steps.update()
                             .where(steps.c.id == copy_id, steps.c.updated_at == copied_at)
                             .values(updated_at=changed_at))


def downgrade() -> None:
    pass

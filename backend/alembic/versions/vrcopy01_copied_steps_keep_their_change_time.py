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
time) gets back the updated_at of the step it was copied from. dashfc08
copied, for each query of the conversation it published, the query's default
step; the conversation is the one of the version it published
(artifact_releases.source_version_id -> artifact_versions.report_id, written
once). The source is the default step of a query of that conversation with
the same title and the same data (copied as it is, byte for byte, encrypted
or not). artifacts.report_id is no guide: the app clears it when the
conversation is deleted and moves it when another conversation takes the
dashboard over. Only where the release lost its version (the foreign key
sets NULL) is the conversation read from the dashboard: the one dashfc08
recorded (legacy_report_id), else its current one.

A copied result is then judged as the conversation judged it before the
upgrade: served if it ran after its step last changed, refused if not. A
copy refreshed since the upgrade keeps its time, and with it refuses the
results it outdated. A copy whose source no longer matches (changed or gone
since) is left as it is: its results stay refused. A step the query no
longer shows by default (an older run of it) is never a source, whatever it
holds. Where the default steps of more than one query of the conversation
match (the same title and data), the latest of their times is kept.

Only rows dashfc08 recorded as its own inserts (dashfc08_undo) are read,
with no application code; what was read and done is logged. Idempotent: a
copy given its source's time no longer has updated_at == created_at. Before
dashfc08 has run (the dashboard corpus applies later revisions unstamped)
there is nothing to do. The downgrade leaves the times as they are: they
are the copies' true times, and dashfc08's own downgrade deletes the copies.
"""
import json
import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "vrcopy01"
down_revision: str | None = "vrstamp01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

UNDO = "dashfc08_undo"
TABLES = (UNDO, "steps", "queries", "artifact_releases", "artifact_versions", "artifacts")
CHUNK = 500


def upgrade() -> None:
    conn = op.get_bind()
    present = set(sa.inspect(conn).get_table_names())
    if not set(TABLES) <= present:
        if {"steps", "artifacts"} <= present and UNDO not in present:
            log.warning("vrcopy01: no %s table: dashfc08 has not run here, so no step copies to restore", UNDO)
        return
    meta = sa.MetaData()
    undo, steps, queries, releases, versions, artifacts = (sa.Table(n, meta, autoload_with=conn) for n in TABLES)
    copied = [str(r[0]) for r in conn.execute(
        sa.select(undo.c.row_id).where(undo.c.table_name == "steps", undo.c.action == "insert")).all()]

    cp, cq = steps.alias("cp"), queries.alias("cq")
    src, sq = steps.alias("src"), queries.alias("sq")
    # The conversation dashfc08 published: its version's (written once).
    conversation = sa.func.coalesce(versions.c.report_id, artifacts.c.legacy_report_id, artifacts.c.report_id)
    same_data = sa.case((sa.cast(src.c.data, sa.Text) == sa.cast(cp.c.data, sa.Text), 1), else_=0)
    counts = {"read": 0, "restored": 0, "no_source": 0, "source_changed_since": 0, "copy_refreshed": 0}
    for i in range(0, len(copied), CHUNK):
        candidates: dict[str, list] = {}
        times = {}
        for copy_id, created_at, copied_at, source_at, same in conn.execute(
            sa.select(cp.c.id, cp.c.created_at, cp.c.updated_at, src.c.updated_at, same_data)
            .select_from(
                cp.join(cq, cq.c.id == cp.c.query_id)
                .join(releases, releases.c.id == cq.c.artifact_release_id)
                .join(artifacts, artifacts.c.id == releases.c.artifact_id)
                .outerjoin(versions, versions.c.id == releases.c.source_version_id)
                .outerjoin(sq, sq.c.report_id == conversation)
                # Only a query's default step was copied (dashfc08 _deps).
                .outerjoin(src, sa.and_(src.c.id == sq.c.default_step_id, src.c.id != cp.c.id,
                                        src.c.title == cp.c.title)))
            .where(cp.c.id.in_(copied[i:i + CHUNK]))
        ).all():
            times[str(copy_id)] = (created_at, copied_at)
            found = candidates.setdefault(str(copy_id), [])
            if source_at is not None:
                found.append((source_at, bool(same)))
        for copy_id, (created_at, copied_at) in times.items():
            counts["read"] += 1
            if copied_at != created_at:
                counts["copy_refreshed"] += 1
                continue
            found = candidates[copy_id]
            if not found:
                counts["no_source"] += 1
                continue
            matching = [at for at, same in found if same]
            changed_at = max(matching) if matching else None
            if changed_at is None or not changed_at < copied_at:
                counts["source_changed_since"] += 1
                continue
            done = conn.execute(steps.update()
                                .where(steps.c.id == copy_id, steps.c.updated_at == copied_at)
                                .values(updated_at=changed_at))
            counts["restored" if done.rowcount else "copy_refreshed"] += 1
    log.info("vrcopy01: %s", json.dumps({
        "copies_read": counts["read"], "restored": counts["restored"],
        "left_as_they_are": {k: counts[k] for k in ("no_source", "source_changed_since", "copy_refreshed")}}))


def downgrade() -> None:
    pass

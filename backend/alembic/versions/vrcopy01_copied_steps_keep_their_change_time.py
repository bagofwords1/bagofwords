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
once). artifacts.report_id is no guide: the app clears it when the
conversation is deleted and moves it when another conversation takes the
dashboard over. Only where the release lost its version (the foreign key
sets NULL) is the conversation read from the dashboard: the one dashfc08
recorded (legacy_report_id), else its current one.

Nothing dashfc08 wrote names the step a copy came from: dashfc08_undo holds
only the copy's own id, and the copied query and step keep no source id
(report_id is cleared; the query's title, parameters and the step's columns
are copies, which twins share; the order of the undo rows pairs a copied
query with its copied step, not with its source). So the source is found by
title, and only where that leaves no doubt: among the default steps of the
conversation's queries exactly one has the copy's title, it holds the copy's
data (copied as it is, byte for byte, encrypted or not), it changed before
the copy was made, and the release holds no other copy of that title. Then
the copy takes its time.

A copied result is then judged as the conversation judged it before the
upgrade: served if it ran after its step last changed, refused if not. Every
other copy is left as it is, and its results stay refused (a reader's next
run serves them again): a copy refreshed since the upgrade (it keeps its
time, and with it refuses the results it outdated); a copy with no source
left (changed title or gone); a source that changed since (its data or its
time); a step the query no longer shows by default (an older run of it),
which is never a source, whatever it holds; and twins, where more than one
default step of the conversation has the title, or the release more than
one copy of it, matching or not -- once a twin changed or its query moved
to another step, which one a copy came from can no longer be told.

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
    sib, sibq, sibu = steps.alias("sib"), queries.alias("sibq"), undo.alias("sibu")
    # The conversation dashfc08 published: its version's (written once).
    conversation = sa.func.coalesce(versions.c.report_id, artifacts.c.legacy_report_id, artifacts.c.report_id)
    same_data = sa.case((sa.cast(src.c.data, sa.Text) == sa.cast(cp.c.data, sa.Text), 1), else_=0)
    # The copies dashfc08 made into the same release with the same title.
    copies_of_title = (
        sa.select(sa.func.count())
        .select_from(sib.join(sibq, sibq.c.id == sib.c.query_id)
                     .join(sibu, sa.and_(sibu.c.row_id == sib.c.id, sibu.c.table_name == "steps",
                                         sibu.c.action == "insert")))
        .where(sibq.c.artifact_release_id == cq.c.artifact_release_id, sib.c.title == cp.c.title)
        .scalar_subquery())
    reasons = ("rows_gone", "copy_refreshed", "ambiguous_source", "no_source", "source_changed_since")
    counts = dict.fromkeys(("read", "restored", *reasons), 0)
    for i in range(0, len(copied), CHUNK):
        chunk = copied[i:i + CHUNK]
        candidates: dict[str, list] = {}
        times = {}
        for copy_id, created_at, copied_at, copies, source_at, same in conn.execute(
            sa.select(cp.c.id, cp.c.created_at, cp.c.updated_at, copies_of_title, src.c.updated_at, same_data)
            .select_from(
                cp.join(cq, cq.c.id == cp.c.query_id)
                .join(releases, releases.c.id == cq.c.artifact_release_id)
                .join(artifacts, artifacts.c.id == releases.c.artifact_id)
                .outerjoin(versions, versions.c.id == releases.c.source_version_id)
                .outerjoin(sq, sq.c.report_id == conversation)
                # Only a query's default step was copied (dashfc08 _deps).
                .outerjoin(src, sa.and_(src.c.id == sq.c.default_step_id, src.c.id != cp.c.id,
                                        src.c.title == cp.c.title)))
            .where(cp.c.id.in_(chunk))
        ).all():
            times[str(copy_id)] = (created_at, copied_at, copies)
            found = candidates.setdefault(str(copy_id), [])
            if source_at is not None:
                found.append((source_at, bool(same)))
        # A copy whose step, query or release is gone has no row above.
        counts["read"] += len(chunk)
        counts["rows_gone"] += len(set(chunk) - set(times))
        for copy_id, (created_at, copied_at, copies) in times.items():
            found = candidates[copy_id]
            if copied_at != created_at:
                counts["copy_refreshed"] += 1
                continue
            # Twins: more than one step it may have come from. Leave it.
            if len(found) > 1 or copies > 1:
                counts["ambiguous_source"] += 1
                continue
            if not found:
                counts["no_source"] += 1
                continue
            ((changed_at, same),) = found
            if not same or not changed_at < copied_at:
                counts["source_changed_since"] += 1
                continue
            done = conn.execute(steps.update()
                                .where(steps.c.id == copy_id, steps.c.updated_at == copied_at)
                                .values(updated_at=changed_at))
            counts["restored" if done.rowcount else "copy_refreshed"] += 1
    log.info("vrcopy01: %s", json.dumps({
        "copies_read": counts["read"], "restored": counts["restored"],
        "left_as_they_are": {k: counts[k] for k in reasons}}))


def downgrade() -> None:
    pass

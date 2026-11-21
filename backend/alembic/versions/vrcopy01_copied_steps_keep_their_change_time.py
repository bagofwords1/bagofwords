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
title, and only where the conversation's whole history leaves no doubt. The
app never rewrites a title or removes a conversation's query or step: a new
title is a new run (a new step, kept beside the older ones), and a deletion
is a mark. So the query a copy came from still has the title its copy has,
and still holds a step with the title the copied step has. A copy takes a
step's time only where all of this holds:

- one query of the conversation, deleted or not, has the copied query's
  title (dashfc08 copied the query row with it);
- every step with the copied step's title that the conversation ever held
  -- shown by default or not, deleted or not, tied to it by its query, by
  its widget (the app's first versions) or by being a query's default step
  -- is a run of that one query. Runs of one query are one source; a step
  of that title anywhere else in the conversation is a twin, whatever it
  holds and whether or not the dashboard showed it;
- the step that query shows by default has the copied step's title and
  data (copied as it is, byte for byte, encrypted or not; none on both
  sides is the same), and a time, earlier than the copy's. Only a default
  step was copied, and a query moves to a new step only by a new run, which
  is later than the copy: a query that moved on no longer shows the source;
- the release holds no other copy of that title. The rule above already
  refuses where two queries of the conversation were copied under one
  title; this one still refuses if one of them is no longer there to be
  counted (removed by hand: the app does not).

A copied result is then judged as the conversation judged it before the
upgrade: served if it ran after its step last changed, refused if not. Every
other copy is left as it is, and its results stay refused (a reader's next
run serves them again): a copy refreshed since the upgrade (it keeps its
time, and with it refuses the results it outdated); a copy with no source
left (no query of its title, or one that shows another title now); a source
that changed since (its data or its time) or has no time; and twins, as
above -- once a twin changed or its query moved to another step, which one
a copy came from can no longer be told.

Only rows dashfc08 recorded as its own inserts (dashfc08_undo) are read,
with no application code; what was read and done is logged, each copy under
one reason. Idempotent: a copy given its source's time no longer has
updated_at == created_at (a second run counts it as already restored: only
this revision moves a time before the row was made). A time is only moved
earlier, and only while the copy still has the time that was read. Before
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
TABLES = (UNDO, "steps", "queries", "widgets", "artifact_releases", "artifact_versions", "artifacts")
CHUNK = 500
# Why a copy was left as it is; each copy is counted once.
REASONS = ("rows_gone", "already_restored", "copy_refreshed", "ambiguous_source", "no_source",
           "source_time_unknown", "source_changed_since", "copy_changed_meanwhile")


def upgrade() -> None:
    conn = op.get_bind()
    missing = sorted(set(TABLES) - set(sa.inspect(conn).get_table_names()))
    if missing:
        log.warning("vrcopy01: no table %s here: no step copies to restore (before dashfc08 has run "
                    "there are none)", ", ".join(missing))
        return
    meta = sa.MetaData()
    undo, steps, queries, widgets, releases, versions, artifacts = (
        sa.Table(n, meta, autoload_with=conn) for n in TABLES)
    copied = [str(r[0]) for r in conn.execute(
        sa.select(undo.c.row_id).where(undo.c.table_name == "steps", undo.c.action == "insert")).all()]

    cp, cq = steps.alias("cp"), queries.alias("cq")
    src, sq = steps.alias("src"), queries.alias("sq")
    sib, sibq, sibu = steps.alias("sib"), queries.alias("sibq"), undo.alias("sibu")
    held, heldq, heldd = steps.alias("held"), queries.alias("heldq"), queries.alias("heldd")
    heldw = widgets.alias("heldw")
    # The conversation dashfc08 published: its version's (written once).
    conversation = sa.func.coalesce(versions.c.report_id, artifacts.c.legacy_report_id, artifacts.c.report_id)
    same_data = sa.case((sa.cast(src.c.data, sa.Text).is_not_distinct_from(sa.cast(cp.c.data, sa.Text)), 1),
                        else_=0)
    # The copies dashfc08 made into the same release with the same title.
    copies_of_title = (
        sa.select(sa.func.count())
        .select_from(sib.join(sibq, sibq.c.id == sib.c.query_id)
                     .join(sibu, sa.and_(sibu.c.row_id == sib.c.id, sibu.c.table_name == "steps",
                                         sibu.c.action == "insert")))
        .where(sibq.c.artifact_release_id == cq.c.artifact_release_id, sib.c.title == cp.c.title)
        .scalar_subquery())
    # The steps of that title the conversation holds (through their query,
    # their widget, or as a query's default) that are not runs of sq.
    a_run_of_sq = sa.or_(held.c.query_id == sq.c.id, held.c.id == sq.c.default_step_id,
                         sa.and_(held.c.query_id.is_(None), held.c.widget_id == sq.c.widget_id))
    twins = (
        sa.select(sa.func.count())
        .select_from(held.outerjoin(heldq, heldq.c.id == held.c.query_id)
                     .outerjoin(heldw, heldw.c.id == held.c.widget_id)
                     .outerjoin(heldd, heldd.c.default_step_id == held.c.id))
        .where(held.c.title == cp.c.title, a_run_of_sq.is_not(sa.true()),
               sa.or_(heldq.c.report_id == conversation, heldw.c.report_id == conversation,
                      heldd.c.report_id == conversation))
        .scalar_subquery())
    counts = dict.fromkeys(("read", "restored", *REASONS), 0)
    for i in range(0, len(copied), CHUNK):
        chunk = copied[i:i + CHUNK]
        candidates: dict[str, list] = {}
        times = {}
        for copy_id, created_at, copied_at, copies, query_id, others, source_id, source_at, same in conn.execute(
            sa.select(cp.c.id, cp.c.created_at, cp.c.updated_at, copies_of_title, sq.c.id, twins,
                      src.c.id, src.c.updated_at, same_data)
            .select_from(
                cp.join(cq, cq.c.id == cp.c.query_id)
                .join(releases, releases.c.id == cq.c.artifact_release_id)
                .join(artifacts, artifacts.c.id == releases.c.artifact_id)
                .outerjoin(versions, versions.c.id == releases.c.source_version_id)
                # dashfc08 copied the query row: its title is its source's.
                .outerjoin(sq, sa.and_(sq.c.report_id == conversation, sq.c.title == cq.c.title))
                # Only a query's default step was copied (dashfc08 _deps).
                .outerjoin(src, sa.and_(src.c.id == sq.c.default_step_id, src.c.id != cp.c.id,
                                        src.c.title == cp.c.title)))
            .where(cp.c.id.in_(chunk))
        ).all():
            times[str(copy_id)] = (created_at, copied_at, copies)
            if query_id is not None:
                candidates.setdefault(str(copy_id), []).append((others, source_id, source_at, bool(same)))
        # A copy whose step, query or release is gone has no row above.
        counts["read"] += len(chunk)
        counts["rows_gone"] += len(set(chunk) - set(times))
        for copy_id, (created_at, copied_at, copies) in times.items():
            counts[_restore(conn, steps, copy_id, created_at, copied_at, copies, candidates.get(copy_id, []))] += 1
    log.info("vrcopy01: %s", json.dumps({
        "copies_read": counts["read"], "restored": counts["restored"],
        "left_as_they_are": {k: counts[k] for k in REASONS}}))


def _restore(conn, steps, copy_id, created_at, copied_at, copies, queries_of_title) -> str:
    """Give one copy its source's time if the source is certain. What was
    done: "restored", or the reason it was left as it is."""
    if copied_at != created_at:
        # The app only moves a time forward: one before the row was made is ours.
        earlier = copied_at is not None and created_at is not None and copied_at < created_at
        return "already_restored" if earlier else "copy_refreshed"
    # Twins: more than one step it may have come from. Leave it.
    if len(queries_of_title) > 1 or copies > 1 or any(others for others, *_ in queries_of_title):
        return "ambiguous_source"
    if not queries_of_title or queries_of_title[0][1] is None:
        return "no_source"
    ((_, _, changed_at, same),) = queries_of_title
    if changed_at is None:
        return "source_time_unknown"
    if not same or not changed_at < copied_at:
        return "source_changed_since"
    done = conn.execute(steps.update().where(steps.c.id == copy_id, steps.c.updated_at == copied_at)
                        .values(updated_at=changed_at))
    if not done.rowcount:
        log.warning("vrcopy01: step copy %s changed while it was read: left as it is", copy_id)
        return "copy_changed_meanwhile"
    return "restored"


def downgrade() -> None:
    pass

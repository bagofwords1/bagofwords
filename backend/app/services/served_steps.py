"""Which step a query serves.

The step a dashboard renders for a query, a rerun re-executes, and the
viewer policy asks about: the query's default step, else its latest
successful step, else its latest step (by widget; a query without a widget,
a published dashboard's copy, by its own steps). Follows
query_service.get_default_step_for_query -- keep them in sync -- with two
differences, both resolving a step where that function returns none:

- a dangling default (default_step_id names no step): that function stops
  and serves nothing; this falls back to the latest step;
- a query with no widget: that function looks for `widget_id == "None"`
  and finds nothing; this looks at the query's own steps.

Returning a step there is safe for both callers: a rerun has something to
run instead of counting the query as failed, and the viewer policy counts
that step's sources too -- one step more can only widen the sources a
refusal is asked about, never narrow them.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.query import Query
from app.models.step import Step


async def served_steps(db: AsyncSession, query_ids: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """((step_id, code) of the step each query serves, deduped; ids of the
    queries that serve no step at all). Step payloads are not loaded, and
    the common lookups are batched, so the round trips do not grow with the
    number of queries."""
    if not query_ids:
        return [], []
    query_ids = [str(q) for q in query_ids]
    query_rows = (await db.execute(
        select(Query.id, Query.default_step_id, Query.widget_id).where(Query.id.in_(query_ids))
    )).all()

    default_ids = [r[1] for r in query_rows if r[1]]
    code_by_step: dict[str, str] = {}
    if default_ids:
        for sid, code in (await db.execute(
            select(Step.id, Step.code).where(Step.id.in_(default_ids))
        )).all():
            code_by_step[str(sid)] = code

    found = {str(r[0]) for r in query_rows}
    unresolved = [q for q in query_ids if q not in found]  # ids with no Query row
    targets: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(step_id, code) -> None:
        step_id = str(step_id)
        # Two queries can resolve to the same step (a shared widget with no
        # default step): it is served once.
        if step_id not in seen:
            seen.add(step_id)
            targets.append((step_id, code))

    for qid, default_step_id, widget_id in query_rows:
        if default_step_id and str(default_step_id) in code_by_step:
            add(default_step_id, code_by_step[str(default_step_id)])
            continue
        # Rare fallback (no or dangling default step): the latest successful
        # step, else the latest step. "Step.widget_id == None" would match
        # every widget-less step in the database.
        row = None
        for only_success in (True, False):
            stmt = select(Step.id, Step.code).where(
                Step.widget_id == widget_id if widget_id else Step.query_id == qid)
            if only_success:
                stmt = stmt.where(Step.status == "success")
            row = (await db.execute(stmt.order_by(Step.created_at.desc()).limit(1))).first()
            if row:
                break
        if row:
            add(row[0], row[1])
        else:
            unresolved.append(str(qid))
    return targets, unresolved

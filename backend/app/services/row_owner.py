"""Who owns a step or a query: a conversation, a published dashboard, or no one.

Until phase 2 every query and step hung off a widget, and every widget off a
conversation, so code found "the conversation" by walking either link. A
published dashboard's copies have neither (docs/design/dashboards-first-class.md):
they belong to their release through `queries.artifact_release_id`. Every gate
asks here instead of walking the links itself, and refuses whatever is not a
conversation it can check.

The rules reproduce what each kind of caller found before, so no existing row
changes hands:

- a step with a widget: that widget's conversation, exactly as every
  step-keyed check read `step.widget.report` (none when the widget has none);
- a step without a widget (a copy): its query's conversation, else its
  query's release;
- a query: its own `report_id` (how query-keyed checks found it), then its
  widget's conversation, then its release. The two places that reached a
  query's conversation through its widget pass `widget_first=True` and get
  that widget's conversation only, as before. Reaching
  the widget's conversation when `report_id` is empty is the one deliberate
  change (legacy queries, decision 5 of the phase 2 plan).

Each function takes the row or its id.
"""
from dataclasses import dataclass

from sqlalchemy import select

from app.models.query import Query
from app.models.step import Step
from app.models.widget import Widget


@dataclass(frozen=True)
class Owner:
    kind: str  # "report" | "release"
    id: str

    @property
    def report_id(self) -> str | None:
        return self.id if self.kind == "report" else None


def _id(row_or_id) -> str:
    return str(row_or_id) if isinstance(row_or_id, str) else str(row_or_id.id)


def _first(*candidates) -> Owner | None:
    for kind, value in candidates:
        if value:
            return Owner(kind, str(value))
    return None


async def step_owner(db, step) -> Owner | None:
    if step is None:
        return None
    if isinstance(step, str):
        row = (await db.execute(select(Step.widget_id, Step.query_id).where(Step.id == step))).first()
        if row is None:
            return None
        widget_id, query_id = row
    else:
        # The row is in hand: no second read of the steps table, which list
        # and public reads keep to one query per step.
        widget_id, query_id = step.widget_id, step.query_id
    if widget_id:
        return _first(("report", await db.scalar(select(Widget.report_id).where(Widget.id == str(widget_id)))))
    if not query_id:
        return None
    row = (await db.execute(
        select(Query.report_id, Query.artifact_release_id).where(Query.id == str(query_id))
    )).first()
    return _first(("report", row[0]), ("release", row[1])) if row else None


async def query_owner(db, query, *, widget_first: bool = False) -> Owner | None:
    if query is None:
        return None
    own_report = None if isinstance(query, str) else getattr(query, "report_id", None)
    if own_report and not widget_first:
        return Owner("report", str(own_report))  # what query-keyed checks always read
    row = (await db.execute(
        select(Query.widget_id, Query.report_id, Widget.report_id, Query.artifact_release_id)
        .select_from(Query)
        .outerjoin(Widget, Widget.id == Query.widget_id)
        .where(Query.id == _id(query))
    )).first()
    if row is None:
        return None
    widget_id, own, widget, release = row
    if widget_first and widget_id:
        return _first(("report", widget))  # exactly the widget join those paths used
    return _first(("report", own), ("report", widget), ("release", release))


async def step_report_id(db, step) -> str | None:
    """The conversation a step belongs to; None for a copy or an orphan."""
    owner = await step_owner(db, step)
    return owner.report_id if owner else None


async def query_report_id(db, query, *, widget_first: bool = False) -> str | None:
    """The conversation a query belongs to; None for a copy or an orphan."""
    owner = await query_owner(db, query, widget_first=widget_first)
    return owner.report_id if owner else None

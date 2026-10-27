"""No new code finds a conversation through a widget (phase 2, part B).

A published dashboard's copies have no widget, so code that reaches "the
conversation" through one silently finds nothing -- the kind of gap phase 2
closed by sending every gate through app/services/row_owner.py. The lookups
left are listed here with the reason each may stay; a new one, or one that
moved, fails this test until it goes through row_owner or is added below on
purpose.

Marked e2e so it runs in the blocking CI job (the unit job does not block).
"""
import collections
import pathlib
import re

import pytest

APP = pathlib.Path(__file__).resolve().parents[3] / "app"

# A widget's conversation, a step joined to its widget or loaded with its
# widget's report, or "the latest step of this widget".
LOOKUP = re.compile(
    r"\bWidget\.report_id|\.widget\.report\b|\bwidget\.report_id\b|\bWidget\.report\b"
    r"|\bStep\.widget_id\s*==|\bWidget\.id\s*==\s*Step\.widget_id"
)

ALLOWED = {
    # The resolver every gate uses (its docstring names the old lookup once).
    "services/row_owner.py": 3,
    # A conversation's own widgets or steps, listed for that conversation;
    # copies are not part of any conversation.
    "ai/agent_v2.py": 1,                                 # publish the conversation's widgets
    "ai/context/builders/widget_context_builder.py": 2,  # the agent's view of the conversation
    "serializers/completion_v2.py": 1,                   # the step a completion's widget produced
    "services/widget_service.py": 2,                     # legacy widget dashboards
    "services/checkin_service.py": 4,                    # the conversation's steps for check-ins
    "services/console_service.py": 4,                    # console metrics per conversation
    "services/identity_taint.py": 2,                     # load_step targets inside a conversation
    # "The latest step of this query's widget" when the default step is
    # missing, on paths guard_route closes to copies (a copy's "None" matches
    # nothing); the rerun picks the query's own steps when there is no widget.
    "services/query_service.py": 4,
    "services/report_service.py": 1,
    "services/step_service.py": 3,                       # get_step_by_id preloads the widget's report; legacy helper
    # Creating a conversation's query: the widget it names must be that conversation's.
    "routes/query.py": 1,
    # Organization id for chat-platform delivery of a conversation's step;
    # a failure there is caught and nothing is sent.
    "services/slack_notification_service.py": 2,
}


def _lookups():
    found = collections.Counter()
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        if rel.split("/")[0] in ("models", "schemas"):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.lstrip().startswith("#"):
                found[rel] += len(LOOKUP.findall(line))
    return {k: v for k, v in found.items() if v}


@pytest.mark.e2e
def test_conversation_lookups_through_a_widget_are_only_the_known_ones():
    found = _lookups()
    new = {f: n for f, n in found.items() if n > ALLOWED.get(f, 0)}
    gone = {f: n for f, n in ALLOWED.items() if found.get(f, 0) < n}
    assert not new, (f"new conversation lookups through a widget: {new}. Use app/services/row_owner.py "
                     "(a published dashboard's copy has no widget), or list it here with the reason.")
    assert not gone, f"lookups removed: {gone}; lower their count here so the list stays exact"


@pytest.mark.e2e
def test_the_pattern_catches_the_lookups_it_is_meant_to():
    """The test above is only as good as the pattern."""
    for code in ("report = step.widget.report",
                 "select(Widget.report_id).where(Widget.id == x)",
                 ".join(Widget, Step.widget_id == Widget.id)",
                 ".join(Widget, Widget.id == Step.widget_id)",
                 "selectinload(Step.widget).selectinload(Widget.report)",
                 "select(Step).where(Step.widget_id == str(q.widget_id))"):
        assert LOOKUP.search(code), code
    for code in ("delete(TextWidget).where(TextWidget.report_id == r)", "Step.query_id == q.id"):
        assert not LOOKUP.search(code), code

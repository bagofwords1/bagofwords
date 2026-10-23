"""The charts an agent's saved version changed (collab01 presence).

A tool's input rarely names the charts it touches -- an edit is find/replace
over the page code -- so after a save the outline comes from the version
itself: its chart fingerprints against the version it grew from.
"""
from app.services.dashboard_presence import changed_keys


def test_added_changed_and_replaced_charts_are_outlined_unchanged_ones_are_not():
    base = {"a": {"fp": "1", "title": "Revenue"}, "b": {"fp": "2", "title": "Artists"},
            "g8": {"fp": "4", "title": "Top 8 genres"}}
    new = {"a": {"fp": "1", "title": "Revenue"}, "b": {"fp": "9", "title": "Top 5 artists"},
           "g6": {"fp": "3", "title": "Top 6 genres"}}
    assert changed_keys(new, base) == ["b", "g6", "g8"]


def test_a_first_version_outlines_every_chart_and_nothing_saved_outlines_none():
    assert changed_keys({"a": {"fp": "1"}}, None) == ["a"]
    assert changed_keys(None, None) == []
    assert changed_keys({}, {}) == []

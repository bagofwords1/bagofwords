"""The 3-way rule for field-level review changes (app.services.field_changes):
a change is the suggestion's intent (base -> proposed), live only while main
lacks it and no reviewer resolved it — the same rule text hunks follow."""
import pytest

from app.services.field_changes import (
    field_change_key,
    field_changes_against_main,
    field_meta,
    is_field_key,
)


def meta(status="published", load_mode="always"):
    return field_meta(status, load_mode)


@pytest.mark.parametrize("field,frm,to", [
    ("status", "published", "draft"),
    ("status", "draft", "published"),
    ("load_mode", "always", "intelligent"),
    ("load_mode", "intelligent", "always"),
])
def test_intended_change_against_unchanged_main_is_live(field, frm, to):
    base = {**meta(), field: frm}
    proposed = {**base, field: to}
    changes = field_changes_against_main(base, proposed, dict(base))
    assert [(c["field"], c["from"], c["to"]) for c in changes] == [(field, frm, to)]
    assert is_field_key(changes[0]["key"])


def test_no_intent_is_not_a_change_even_when_main_moved():
    # The suggestion carried the base's status; main was switched off since.
    # Proposing "back on" would be a change nobody asked for.
    base = proposed = meta(status="published")
    assert field_changes_against_main(base, proposed, meta(status="draft")) == []


def test_change_main_already_carries_is_not_live():
    assert field_changes_against_main(
        meta(status="published"), meta(status="draft"), meta(status="draft")) == []


def test_resolved_change_is_not_live_but_a_new_target_is():
    base, proposed, main = meta(), meta(status="draft"), meta()
    key = field_change_key("status", "draft")
    assert field_changes_against_main(base, proposed, main, {key}) == []
    # A different proposed value is a different decision.
    assert field_changes_against_main(
        base, meta(load_mode="intelligent"), main, {key})[0]["field"] == "load_mode"


def test_nothing_to_change_on_an_instruction_that_is_not_live():
    # main is None for a create suggestion: the whole proposal is new text.
    assert field_changes_against_main(None, meta(status="draft"), None) == []


def test_archiving_is_never_a_reviewable_suggestion():
    assert field_changes_against_main(meta(), meta(status="archived"), meta()) == []


def test_null_columns_read_as_their_defaults():
    assert field_meta(None, None) == meta()
    assert field_changes_against_main(field_meta(None, None), meta(), meta()) == []

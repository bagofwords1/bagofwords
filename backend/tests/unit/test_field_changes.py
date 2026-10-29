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


# --- scope: agents + tables, reviewed as one version field -------------------

def _vmeta(ds=None, refs=None, status=None, load_mode=None):
    """A proposed or base version's reviewable metadata — scope is carried on
    the version's data_source_ids / references_json."""
    return field_meta(status, load_mode, ds, refs)


def _main(live_ds=(), live_refs=None, status=None, load_mode=None):
    """Main metadata with the live attachment, as InstructionService builds it."""
    return {**field_meta(status, load_mode), "scope_live": tuple(live_ds),
            "scope_live_refs": list(live_refs or [])}


_TA = {"object_type": "datasource_table", "object_id": "t_a", "column_name": None}
_TB = {"object_type": "datasource_table", "object_id": "t_b", "column_name": None}


def test_scope_change_is_one_row_against_live():
    from app.services.field_changes import field_changes_against_main, scope_change_key, SCOPE_FIELD
    base = _vmeta(ds=["a", "c"])
    proposed = _vmeta(ds=["a", "b", "c"])
    rows = field_changes_against_main(base, proposed, _main(live_ds=("a", "c")))
    assert len(rows) == 1 and rows[0]["field"] == SCOPE_FIELD
    assert rows[0]["added"] == ["b"]
    assert rows[0]["from"] == ["a", "c"] and rows[0]["to"] == ["a", "b", "c"]
    assert rows[0]["key"] == scope_change_key(["a", "b", "c"])


def test_scope_key_binds_the_proposed_scope_not_the_order():
    from app.services.field_changes import scope_change_key
    assert scope_change_key(["a", "b"]) == scope_change_key(["b", "a"])
    assert scope_change_key(["a", "b"]) != scope_change_key(["a", "c"])


def test_resolved_scope_change_is_not_live():
    from app.services.field_changes import field_changes_against_main, scope_change_key
    base, proposed = _vmeta(ds=["a"]), _vmeta(ds=["a", "b"])
    key = scope_change_key(["a", "b"])
    assert field_changes_against_main(base, proposed, _main(live_ds=("a",)), {key}) == []


def test_scope_not_inferred_without_base_intent():
    """A version whose scope matches its base proposes nothing, even when it
    disagrees with the live attachment (a stale snapshot is not a proposal)."""
    from app.services.field_changes import field_changes_against_main
    base = proposed = _vmeta(ds=["a", "b"])
    assert field_changes_against_main(base, proposed, _main(live_ds=("a",))) == []


def test_narrowing_a_global_is_reviewable():
    """A suggestion that itself started GLOBAL (base scope empty) and now names
    agents is a real narrowing — shown as a scope row even though live is
    global. The 'from' side reads as all agents (empty)."""
    from app.services.field_changes import field_changes_against_main, SCOPE_FIELD
    rows = field_changes_against_main(_vmeta(ds=[]), _vmeta(ds=["b"]), _main(live_ds=()))
    assert len(rows) == 1 and rows[0]["field"] == SCOPE_FIELD
    assert rows[0]["from"] == [] and rows[0]["to"] == ["b"] and rows[0]["added"] == ["b"]


def test_stale_share_on_a_now_global_instruction_is_moot():
    """A suggestion whose base was SCOPED ([a]) proposing to add an agent is
    stale once the instruction was made global independently: its intent was
    about specific agents, which is moot against 'every agent'. No row."""
    from app.services.field_changes import field_changes_against_main
    assert field_changes_against_main(_vmeta(ds=["a"]), _vmeta(ds=["a", "b"]), _main(live_ds=())) == []


def test_global_carried_forward_proposes_nothing():
    """A global instruction that stays global (base global, proposed global,
    live global) is a no-op: no scope row."""
    from app.services.field_changes import field_changes_against_main
    assert field_changes_against_main(_vmeta(ds=[]), _vmeta(ds=[]), _main(live_ds=())) == []


def test_scope_change_carries_added_table_refs():
    from app.services.field_changes import field_changes_against_main, SCOPE_FIELD
    base = _vmeta(ds=["a"], refs=[_TA])
    proposed = _vmeta(ds=["a", "b"], refs=[_TA, _TB])
    rows = field_changes_against_main(base, proposed, _main(live_ds=("a",), live_refs=[_TA]))
    assert len(rows) == 1 and rows[0]["field"] == SCOPE_FIELD
    assert rows[0]["added"] == ["b"]
    assert [r["object_id"] for r in rows[0]["added_refs"]] == ["t_b"]


def test_table_only_scope_change_adds_no_agent():
    """table_names can change tables without changing agents — still one scope
    row, with the ref delta and no agent added."""
    from app.services.field_changes import field_changes_against_main
    base = _vmeta(ds=["a"], refs=[_TA])
    proposed = _vmeta(ds=["a"], refs=[_TA, _TB])
    rows = field_changes_against_main(base, proposed, _main(live_ds=("a",), live_refs=[_TA]))
    assert len(rows) == 1
    assert rows[0]["added"] == [] and [r["object_id"] for r in rows[0]["added_refs"]] == ["t_b"]

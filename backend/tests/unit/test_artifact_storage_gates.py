"""Pure storage gates for the artifact tools (declaration, references, diff).

The tools are mechanical: an invalid declaration or a `useCollection` call
the declaration does not back must be rejected before anything persists, and
every declaration change that can hide or expose records must be detected
(the confirmation flow consumes the detected changes).
"""
import pytest

from app.ai.tools.implementations._artifact_storage import (
    USE_COLLECTION_RE,
    StorageChange,
    describe_storage_changes,
    referenced_collections,
    storage_changes,
    storage_declaration_errors,
    storage_reference_errors,
)
from app.schemas.app_storage import parse_storage_declaration


def _decl(collections):
    return parse_storage_declaration({"collections": collections})


NOTES = {
    "scope": "shared", "create": "members", "modify": "author",
    "fields": {"country": {"type": "string", "required": True}, "text": {"type": "string"}},
}
PREFS = {"scope": "per_user", "fields": {"genre": {"type": "string", "default": None}}}


# ---------------------------------------------------------------------------
# referenced_collections / storage_reference_errors
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code, expected", [
    ('const n = useCollection("notes");', ["notes"]),
    ("const n = useCollection('notes');", ["notes"]),
    ("const n = useCollection(`notes`);", ["notes"]),
    ('const n = window.useCollection( "notes" );', ["notes"]),
    ('useCollection("a"); useCollection("b"); useCollection("a")', ["a", "b"]),
    ("function App() { return null }", []),
])
def test_referenced_collections_lists_literal_names_once_in_order(code, expected):
    assert referenced_collections(code) == expected


def test_use_collection_regex_matches_calls_only():
    assert USE_COLLECTION_RE.search('x = useCollection("notes")')
    assert not USE_COLLECTION_RE.search("const collection = myuseCollectionHelper")


def test_declared_references_pass():
    decl = _decl({"notes": NOTES, "prefs": PREFS})
    code = 'const n = useCollection("notes"); const p = useCollection(\'prefs\');'
    assert storage_reference_errors(code, decl) == []


def test_code_without_use_collection_passes_with_or_without_declaration():
    assert storage_reference_errors("function App() { return null }", None) == []
    assert storage_reference_errors("function App() { return null }", _decl({"notes": NOTES})) == []


@pytest.mark.parametrize("decl_collections", [None, {"prefs": PREFS}])
def test_undeclared_collection_is_rejected(decl_collections):
    decl = _decl(decl_collections) if decl_collections is not None else None
    errors = storage_reference_errors('const n = useCollection("notes");', decl)
    assert len(errors) == 1
    assert errors[0].startswith("[storage]")
    assert "notes" in errors[0]


@pytest.mark.parametrize("call", [
    "useCollection(name)",
    'useCollection("no" + "tes")',
    "useCollection(`${prefix}notes`)",
    "useCollection()",
])
def test_non_literal_collection_argument_is_rejected(call):
    decl = _decl({"notes": NOTES})
    errors = storage_reference_errors(f"const n = {call};", decl)
    assert errors, f"{call} must be rejected"
    assert all(e.startswith("[storage]") for e in errors)
    assert any("literal" in e for e in errors)


# ---------------------------------------------------------------------------
# storage_declaration_errors
# ---------------------------------------------------------------------------

def test_valid_declaration_has_no_errors():
    assert storage_declaration_errors({"collections": {"notes": NOTES}}, None) == []


def test_none_means_nothing_supplied():
    assert storage_declaration_errors(None, _decl({"notes": NOTES})) == []


@pytest.mark.parametrize("raw", [
    {"collections": {"notes": {**NOTES, "modify": "anyone"}}},             # invalid rule
    {"collections": {"notes": {**NOTES, "fields": {"x": {"type": "text"}}}}},  # invalid type
    {"collections": {"notes": {**NOTES, "extra": 1}}},                    # unknown key
    {"collections": {"notes": NOTES}, "version": 2},                      # unknown top-level key
    {"collections": {"Notes": NOTES}},                                    # bad collection name
    {"collections": {"notes": {"scope": "shared", "fields": NOTES["fields"]}}},  # shared without rules
    {"collections": {"notes": {**NOTES, "fields": {}}}},                  # no fields
    "notes",                                                              # not an object
])
def test_invalid_declaration_is_rejected(raw):
    errors = storage_declaration_errors(raw, None)
    assert errors
    assert all(e.startswith("[storage]") for e in errors)


def test_new_required_field_without_default_in_existing_collection_is_rejected():
    previous = _decl({"notes": NOTES})
    new_fields = {**NOTES["fields"], "rating": {"type": "number", "required": True}}
    errors = storage_declaration_errors({"collections": {"notes": {**NOTES, "fields": new_fields}}}, previous)
    assert len(errors) == 1
    assert errors[0].startswith("[storage]")
    assert "rating" in errors[0] and "notes" in errors[0]


@pytest.mark.parametrize("field", [
    {"type": "number"},                                   # optional
    {"type": "number", "required": True, "default": 0},   # required with default
])
def test_new_optional_or_defaulted_field_in_existing_collection_is_allowed(field):
    previous = _decl({"notes": NOTES})
    new_fields = {**NOTES["fields"], "rating": field}
    assert storage_declaration_errors({"collections": {"notes": {**NOTES, "fields": new_fields}}}, previous) == []


def test_required_field_in_a_new_collection_is_allowed():
    previous = _decl({"prefs": PREFS})
    assert storage_declaration_errors({"collections": {"prefs": PREFS, "notes": NOTES}}, previous) == []


# ---------------------------------------------------------------------------
# storage_changes / describe_storage_changes
# ---------------------------------------------------------------------------

def _kinds(changes):
    return sorted((c.kind, c.collection, c.field) for c in changes)


def test_no_previous_declaration_means_no_changes():
    assert storage_changes(None, _decl({"notes": NOTES}), {}) == []


def test_identical_declaration_has_no_changes():
    assert storage_changes(_decl({"notes": NOTES}), _decl({"notes": NOTES}), {"notes": {"records": 3, "users": 2}}) == []


def test_removed_collection_carries_record_impact():
    changes = storage_changes(
        _decl({"notes": NOTES, "prefs": PREFS}), _decl({"prefs": PREFS}),
        {"notes": {"records": 7, "users": 3}, "prefs": {"records": 1, "users": 1}},
    )
    assert len(changes) == 1
    c = changes[0]
    assert (c.kind, c.collection, c.records, c.users) == ("collection_removed", "notes", 7, 3)


def test_removing_an_empty_collection_is_still_a_change_with_zero_impact():
    changes = storage_changes(_decl({"notes": NOTES}), _decl({"prefs": PREFS}), {})
    assert [(c.kind, c.records, c.users) for c in changes] == [("collection_removed", 0, 0)]


def test_new_none_removes_every_collection():
    changes = storage_changes(_decl({"notes": NOTES, "prefs": PREFS}), None, {})
    assert _kinds(changes) == [("collection_removed", "notes", None), ("collection_removed", "prefs", None)]


def test_field_removed_and_type_changed():
    new_fields = {"country": {"type": "number", "required": True, "default": 0}}
    changes = storage_changes(
        _decl({"notes": NOTES}), _decl({"notes": {**NOTES, "fields": new_fields}}),
        {"notes": {"records": 4, "users": 2}},
    )
    assert _kinds(changes) == [("field_removed", "notes", "text"), ("field_type_changed", "notes", "country")]
    changed = next(c for c in changes if c.kind == "field_type_changed")
    assert (changed.before, changed.after) == ("string", "number")
    assert all((c.records, c.users) == (4, 2) for c in changes)


def test_scope_change_is_detected():
    shared_prefs = {"scope": "shared", "create": "members", "modify": "author", "fields": PREFS["fields"]}
    changes = storage_changes(_decl({"prefs": PREFS}), _decl({"prefs": shared_prefs}), {})
    assert [(c.kind, c.before, c.after) for c in changes] == [("scope_changed", "per_user", "shared")]


def test_create_rule_change_is_detected():
    changes = storage_changes(_decl({"notes": NOTES}), _decl({"notes": {**NOTES, "create": "owner"}}), {})
    assert [(c.kind, c.before, c.after) for c in changes] == [("create_changed", "members", "owner")]


def test_optional_field_made_required_without_default_is_detected():
    new_fields = {**NOTES["fields"], "text": {"type": "string", "required": True}}
    changes = storage_changes(_decl({"notes": NOTES}), _decl({"notes": {**NOTES, "fields": new_fields}}), {})
    assert _kinds(changes) == [("field_made_required", "notes", "text")]


@pytest.mark.parametrize("new_text", [
    {"type": "string", "required": True, "default": "n/a"},  # required but defaulted
    {"type": "string", "max_length": 200},                   # still optional
])
def test_safe_field_changes_are_not_reported(new_text):
    new_fields = {**NOTES["fields"], "text": new_text}
    extra = {"rating": {"type": "number"}}
    changes = storage_changes(
        _decl({"notes": NOTES}), _decl({"notes": {**NOTES, "fields": {**new_fields, **extra}}, "prefs": PREFS}), {},
    )
    assert changes == []


def test_describe_names_each_change_and_its_impact():
    changes = [
        StorageChange(kind="collection_removed", collection="notes", records=12, users=3),
        StorageChange(kind="field_type_changed", collection="prefs", field="genre", before="string", after="number", records=0, users=0),
    ]
    text = describe_storage_changes(changes)
    assert "notes" in text and "12" in text and "3" in text
    assert "genre" in text and "string" in text and "number" in text
    assert describe_storage_changes([]) == ""


# ---------------------------------------------------------------------------
# Tool input schemas
# ---------------------------------------------------------------------------

def test_edit_input_needs_edits_or_storage():
    from pydantic import ValidationError

    from app.ai.tools.schemas.edit_artifact import EditArtifactInput

    with pytest.raises(ValidationError):
        EditArtifactInput(artifact_id="a")
    with pytest.raises(ValidationError):
        EditArtifactInput(artifact_id="a", edits=[])
    storage_only = EditArtifactInput(artifact_id="a", storage={"collections": {}})
    assert storage_only.edits == [] and storage_only.storage == {"collections": {}}
    ops_only = EditArtifactInput(artifact_id="a", edits=[{"find": "x", "replace": "y"}])
    assert ops_only.storage is None


def test_create_input_accepts_storage():
    from app.ai.tools.schemas.create_artifact import CreateArtifactInput

    assert CreateArtifactInput(prompt="p").storage is None
    assert CreateArtifactInput(prompt="p", storage={"collections": {}}).storage == {"collections": {}}


# ---------------------------------------------------------------------------
# W3 remediation: orphaned rows, reference forms, approval budget
# ---------------------------------------------------------------------------

OWNER_NOTES = {**NOTES, "create": "owner"}


def test_readding_a_removed_collection_with_orphaned_rows_is_a_change():
    # notes was per_user, then removed (rows kept); re-declaring it as a shared
    # owner-create collection would expose those private rows to everyone.
    changes = storage_changes(
        _decl({"prefs": PREFS}), _decl({"prefs": PREFS, "notes": OWNER_NOTES}),
        {"notes": {"records": 3, "users": 2}},
    )
    assert [(c.kind, c.collection, c.before, c.after, c.records, c.users) for c in changes] == [
        ("collection_readded", "notes", "orphaned records", "shared/owner", 3, 2),
    ]


def test_orphaned_rows_are_a_change_even_without_a_previous_declaration():
    changes = storage_changes(None, _decl({"notes": {"scope": "per_user", "fields": NOTES["fields"]}}),
                              {"notes": {"records": 1, "users": 1}})
    assert [(c.kind, c.after, c.records) for c in changes] == [("collection_readded", "per_user", 1)]


def test_adding_a_collection_without_rows_is_not_a_change():
    assert storage_changes(None, _decl({"notes": NOTES}), {"prefs": {"records": 4, "users": 1}}) == []
    assert storage_changes(_decl({"prefs": PREFS}), _decl({"prefs": PREFS, "notes": NOTES}), {}) == []


def test_readding_a_field_with_a_different_type_is_a_type_change():
    # text was a json field in an earlier version, removed (values kept),
    # and is now re-added as a string: stored values keep their old type.
    changes = storage_changes(
        _decl({"notes": {**NOTES, "fields": {"country": NOTES["fields"]["country"]}}}),
        _decl({"notes": NOTES}),
        {"notes": {"records": 5, "users": 2}},
        {"notes": {"text": "json"}},
    )
    assert [(c.kind, c.field, c.before, c.after, c.records) for c in changes] == [
        ("field_type_changed", "text", "json", "string", 5),
    ]


def test_readding_a_field_with_the_same_type_is_not_a_change():
    changes = storage_changes(
        _decl({"notes": {**NOTES, "fields": {"country": NOTES["fields"]["country"]}}}),
        _decl({"notes": NOTES}), {"notes": {"records": 5, "users": 2}}, {"notes": {"text": "string"}},
    )
    assert changes == []


def test_describe_readded_collection():
    text = describe_storage_changes([StorageChange(
        kind="collection_readded", collection="notes", before="orphaned records", after="shared/owner",
        records=3, users=2,
    )])
    assert "notes" in text and "shared/owner" in text and "3 record(s)" in text


@pytest.mark.parametrize("code", [
    'const uc = useCollection; const n = uc("notes");',          # alias
    'const n = useCollection?.("notes");',                        # optional call
    'const n = window["useCollection"]("notes");',                # bracket access
    "const n = window['useCollection']('notes');",
    'const { useCollection: uc } = window; uc("notes");',         # destructuring rename
    'const n = useCollection.call(null, "notes");',
])
def test_indirect_use_collection_references_are_rejected(code):
    errors = storage_reference_errors(code, _decl({"notes": NOTES}))
    assert errors, code
    assert all(e.startswith("[storage]") for e in errors)
    assert any("direct" in e for e in errors), errors


def test_comment_between_name_and_paren_is_still_gated():
    errors = storage_reference_errors('const n = useCollection /* x */ ("ghost");', _decl({"notes": NOTES}))
    assert len(errors) == 1 and "ghost" in errors[0]
    assert referenced_collections('useCollection // c\n ("notes")') == ["notes"]


@pytest.mark.parametrize("code", [
    '// useCollection("ghost") is how you would read records\nfunction App() { return null }',
    '/* useCollection(name) */ function App() { return null }',
    'const hint = "call useCollection(\\"ghost\\") to store"; const t = \'see useCollection(x)\';',
    "const doc = `useCollection(ghost)`;",
])
def test_mentions_in_comments_and_strings_are_ignored(code):
    assert storage_reference_errors(code, _decl({"notes": NOTES})) == []
    assert referenced_collections(code) == []


def test_calls_inside_template_interpolation_are_gated():
    errors = storage_reference_errors('const s = `${useCollection("ghost").items.length} notes`;', _decl({"notes": NOTES}))
    assert len(errors) == 1 and "ghost" in errors[0]


def test_jsx_text_apostrophes_do_not_hide_calls():
    code = (
        "function App() {\n"
        "  const x = <p>Don't forget</p>;\n"
        '  const n = useCollection("ghost");\n'
        "  return <p>It's here</p>;\n"
        "}\n"
    )
    errors = storage_reference_errors(code, _decl({"notes": NOTES}))
    assert len(errors) == 1 and "ghost" in errors[0]


def test_unterminated_template_falls_back_to_scanning_everything():
    # A stray backtick must not mask the rest of the file.
    code = 'const a = <p>use `x</p>;\nconst n = useCollection("ghost");'
    errors = storage_reference_errors(code, _decl({"notes": NOTES}))
    assert any("ghost" in e for e in errors)


def test_confirmation_wait_budget_uses_the_absolute_deadline():
    from app.ai.tools.implementations._artifact_storage import (
        STORAGE_CONFIRM_SAFETY_MARGIN_S,
        STORAGE_CONFIRM_TIMEOUT_S,
        confirmation_wait_budget,
    )
    now = 1000.0
    # No deadline known: the full configured wait.
    assert confirmation_wait_budget(None, now) == STORAGE_CONFIRM_TIMEOUT_S
    # Plenty of budget: capped at the configured wait.
    assert confirmation_wait_budget(now + 600, now) == STORAGE_CONFIRM_TIMEOUT_S
    # 100 s left of the runner's deadline: wait what remains after the margin.
    assert confirmation_wait_budget(now + 100, now) == 100 - STORAGE_CONFIRM_SAFETY_MARGIN_S
    # Too little left to be answerable (a retry late in the budget): None.
    assert confirmation_wait_budget(now + 25, now) is None
    assert confirmation_wait_budget(now - 5, now) is None


def test_confirmation_deadline_prefers_the_runner_deadline():
    from app.ai.tools.implementations._artifact_storage import TOOL_HARD_TIMEOUT_S, confirmation_deadline
    from app.ai.runner.policies import TimeoutPolicy

    assert confirmation_deadline({"tool_deadline_monotonic": 42.0}, started_monotonic=10.0) == 42.0
    assert confirmation_deadline({}, started_monotonic=10.0) == 10.0 + TOOL_HARD_TIMEOUT_S
    assert confirmation_deadline({}, started_monotonic=None) is None
    assert TOOL_HARD_TIMEOUT_S == float(TimeoutPolicy().hard_timeout_s)


def test_stopped_failure_text():
    from app.ai.tools.implementations._artifact_storage import storage_confirmation_failure
    text = storage_confirmation_failure("stopped", [])
    assert "stopped" in text.lower() and "nothing was applied" in text


# ---------------------------------------------------------------------------
# Planner guidance: misplaced storage, write handling, effective rules
# ---------------------------------------------------------------------------

_MISPLACED = "It looks like the storage declaration was placed inside `prompt`"


@pytest.mark.parametrize("prompt", [
    'Build it. {"replaces_artifact_id":null,"storage":{"collections":{"prefs":{"scope":"per_user"}}}}',
    "Genre picker, storage={collections: {prefs: ...}}",
])
def test_misplaced_storage_hint_is_appended_when_prompt_carries_storage(prompt):
    from app.ai.tools.implementations._artifact_storage import with_misplaced_storage_hint

    errors = storage_reference_errors('const p = useCollection("prefs");', None)
    hinted = with_misplaced_storage_hint(errors, prompt)
    assert len(hinted) == 1
    assert hinted[0].startswith(errors[0])
    assert _MISPLACED in hinted[0]
    assert "separate top-level `storage` argument" in hinted[0]


@pytest.mark.parametrize("prompt", ["", None, "A genre dashboard that remembers the selection"])
def test_misplaced_storage_hint_absent_without_storage_in_prompt(prompt):
    from app.ai.tools.implementations._artifact_storage import with_misplaced_storage_hint

    errors = storage_reference_errors('const p = useCollection("prefs");', None)
    assert with_misplaced_storage_hint(errors, prompt) == errors


def test_misplaced_storage_hint_leaves_no_errors_alone():
    from app.ai.tools.implementations._artifact_storage import with_misplaced_storage_hint

    assert with_misplaced_storage_hint([], '"storage": {}') == []


@pytest.mark.parametrize("code", [
    'const c = useCollection("comments"); const save = () => c.add({ text });',
    'const { add } = useCollection("comments"); const save = () => add({ text });',
    'const c = useCollection("comments"); const del = (id) => c.remove(id);',
    'const c = useCollection("comments"); c.update(id, { text }); // .catch( in a comment does not count',
])
def test_write_handling_note_when_writes_have_no_catch(code):
    from app.ai.tools.implementations._artifact_storage import write_handling_note

    note = write_handling_note(code)
    assert note.startswith(" NOTE:")
    assert "catch" in note and "error" in note and "edit_artifact" in note


@pytest.mark.parametrize("code", [
    'const c = useCollection("comments"); const save = () => c.add({ text }).catch(() => {});',
    'const c = useCollection("comments"); const save = async () => { try { await c.add({ text }); } catch (e) {} };',
    'const c = useCollection("comments"); return c.items.length;',  # reads only
    "const s = new Set(); s.add(1);",  # no useCollection at all
])
def test_no_write_handling_note_when_handled_or_not_writing(code):
    from app.ai.tools.implementations._artifact_storage import write_handling_note

    assert write_handling_note(code) == ""


def test_describe_storage_rules_in_plain_words():
    from app.ai.tools.implementations._artifact_storage import describe_storage_rules

    decl = _decl({
        "comments": {"scope": "shared", "create": "owner", "modify": "owner", "fields": {"text": {"type": "string"}}},
        "notes": NOTES,
        "prefs": PREFS,
    })
    assert describe_storage_rules(decl) == (
        "Storage: comments — shared; add: owner only; edit/delete: owner only. "
        "notes — shared; add: members; edit/delete: each author their own (owner: any). "
        "prefs — private per viewer."
    )


def test_describe_storage_rules_empty_without_collections():
    from app.ai.tools.implementations._artifact_storage import describe_storage_rules

    assert describe_storage_rules(None) == ""
    assert describe_storage_rules(_decl({})) == ""

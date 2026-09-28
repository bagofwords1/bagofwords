"""Contract tests for the pure Layer-2 policy and record validation (services/app_data_rules.py).

Layer 1 (artifact visibility) is enforced by the service before these rules
run. Here the full collection-kind x principal x operation decision table is
pinned cell by cell, together with record validation, size limits, and
read-time projection (E1 and E2 items 10-17 of the plan).
"""
import json

import pytest

from app.schemas.app_storage import (
    MAX_JSON_FIELD_BYTES,
    MAX_RECORD_BYTES,
    MAX_RECORD_TOTAL_BYTES,
    CollectionSpec,
)
from app.services.app_data_rules import (
    RecordValidationError,
    can_create,
    can_modify,
    can_read,
    project_record_data,
    validate_new_record,
    validate_record_patch,
    visible_author_filter,
)

OWNER_ID = "user-owner"
MEMBER_ID = "user-member"
OUTSIDER_ID = "user-outsider"
OTHER_ID = "user-somebody-else"

PRINCIPAL_USER_ID = {
    "owner": OWNER_ID,
    "member": MEMBER_ID,
    "outsider": OUTSIDER_ID,
    "anonymous": None,
}


def _shared(create: str, modify: str, public_read: bool = False) -> CollectionSpec:
    raw = {"scope": "shared", "create": create, "modify": modify, "fields": {"text": {"type": "string"}}}
    if public_read:
        raw["public_read"] = True
    return CollectionSpec.model_validate(raw)


def _per_user() -> CollectionSpec:
    return CollectionSpec.model_validate({"scope": "per_user", "fields": {"text": {"type": "string"}}})


KINDS = {
    "shared_members_author": _shared("members", "author"),
    "shared_members_owner": _shared("members", "owner"),
    "shared_owner_owner": _shared("owner", "owner"),
    "shared_owner_author": _shared("owner", "author"),
    "shared_owner_owner_public": _shared("owner", "owner", public_read=True),
    "shared_owner_author_public": _shared("owner", "author", public_read=True),
    "per_user": _per_user(),
}

# (read, create, modify_own_record, modify_someone_elses_record), straight from
# the E1 decision table.
NONE = (False, False, False, False)
DECISION_TABLE = {
    ("shared_members_author", "owner"): (True, True, True, True),
    ("shared_members_author", "member"): (True, True, True, False),
    ("shared_members_author", "outsider"): NONE,
    ("shared_members_author", "anonymous"): NONE,
    ("shared_members_owner", "owner"): (True, True, True, True),
    ("shared_members_owner", "member"): (True, True, False, False),
    ("shared_members_owner", "outsider"): NONE,
    ("shared_members_owner", "anonymous"): NONE,
    # create "owner" alone never publishes (Codex #1): public_read decides.
    ("shared_owner_owner", "owner"): (True, True, True, True),
    ("shared_owner_owner", "member"): (True, False, False, False),
    ("shared_owner_owner", "outsider"): NONE,
    ("shared_owner_owner", "anonymous"): NONE,
    ("shared_owner_author", "owner"): (True, True, True, True),
    ("shared_owner_author", "member"): (True, False, False, False),
    ("shared_owner_author", "outsider"): NONE,
    ("shared_owner_author", "anonymous"): NONE,
    ("shared_owner_owner_public", "owner"): (True, True, True, True),
    ("shared_owner_owner_public", "member"): (True, False, False, False),
    ("shared_owner_owner_public", "outsider"): (True, False, False, False),
    ("shared_owner_owner_public", "anonymous"): (True, False, False, False),
    ("shared_owner_author_public", "owner"): (True, True, True, True),
    ("shared_owner_author_public", "member"): (True, False, False, False),
    ("shared_owner_author_public", "outsider"): (True, False, False, False),
    ("shared_owner_author_public", "anonymous"): (True, False, False, False),
    ("per_user", "owner"): (True, True, True, False),
    ("per_user", "member"): (True, True, True, False),
    ("per_user", "outsider"): NONE,
    ("per_user", "anonymous"): NONE,
}


def test_decision_table_covers_every_kind_and_principal():
    assert set(DECISION_TABLE) == {(k, p) for k in KINDS for p in PRINCIPAL_USER_ID}


@pytest.mark.parametrize("kind,principal", sorted(DECISION_TABLE))
class TestDecisionTable:
    def test_read(self, kind, principal):
        assert can_read(KINDS[kind], principal) is DECISION_TABLE[(kind, principal)][0]

    def test_create(self, kind, principal):
        assert can_create(KINDS[kind], principal) is DECISION_TABLE[(kind, principal)][1]

    def test_modify_own_record(self, kind, principal):
        user_id = PRINCIPAL_USER_ID[principal]
        # Anonymous has no id, so "own" is meaningless; its row is the same as "other".
        record_user_id = user_id if user_id is not None else OTHER_ID
        expected = DECISION_TABLE[(kind, principal)][2]
        assert can_modify(KINDS[kind], principal, user_id=user_id, record_user_id=record_user_id) is expected

    def test_modify_someone_elses_record(self, kind, principal):
        user_id = PRINCIPAL_USER_ID[principal]
        expected = DECISION_TABLE[(kind, principal)][3]
        assert can_modify(KINDS[kind], principal, user_id=user_id, record_user_id=OTHER_ID) is expected

    def test_author_filter(self, kind, principal):
        user_id = PRINCIPAL_USER_ID[principal]
        if kind == "per_user" and user_id is None:
            # Fail closed: a private collection is never listed without an author.
            with pytest.raises(ValueError):
                visible_author_filter(KINDS[kind], principal, user_id=user_id, owner_id=OWNER_ID)
            return
        expected = AUTHOR_FILTER[(kind, principal)]
        assert visible_author_filter(KINDS[kind], principal, user_id=user_id, owner_id=OWNER_ID) == expected


# Whose rows a list returns (None = every row), written out per kind/principal:
# per_user -> the caller's own; outsiders and anonymous -> only the report
# owner's rows, whatever the collection held before (Codex #1).
AUTHOR_FILTER = {
    **{(k, "owner"): None for k in KINDS if k != "per_user"},
    **{(k, "member"): None for k in KINDS if k != "per_user"},
    **{(k, "outsider"): OWNER_ID for k in KINDS if k != "per_user"},
    **{(k, "anonymous"): OWNER_ID for k in KINDS if k != "per_user"},
    ("per_user", "owner"): OWNER_ID,
    ("per_user", "member"): MEMBER_ID,
    ("per_user", "outsider"): OUTSIDER_ID,
}


@pytest.mark.parametrize("principal", sorted(PRINCIPAL_USER_ID))
def test_per_user_author_filter_without_user_id_fails_closed(principal):
    with pytest.raises(ValueError):
        visible_author_filter(KINDS["per_user"], principal, user_id=None, owner_id=OWNER_ID)


@pytest.mark.parametrize("principal", ["outsider", "anonymous"])
def test_public_author_filter_without_owner_id_fails_closed(principal):
    with pytest.raises(ValueError):
        visible_author_filter(KINDS["shared_owner_owner_public"], principal,
                              user_id=PRINCIPAL_USER_ID[principal], owner_id=None)


def test_regression_create_members_to_owner_does_not_publish():
    # Codex #1: switching who may add must never change who may read.
    before = _shared("members", "author")
    after = _shared("owner", "author")
    for principal in ("outsider", "anonymous"):
        assert can_read(before, principal) is False
        assert can_read(after, principal) is False
    assert can_read(_shared("owner", "author", public_read=True), "anonymous") is True


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_anonymous_never_writes(kind):
    spec = KINDS[kind]
    assert can_create(spec, "anonymous") is False
    assert can_modify(spec, "anonymous", user_id=None, record_user_id=OTHER_ID) is False


PUBLIC_KINDS = {"shared_owner_owner_public", "shared_owner_author_public"}


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_outsider_or_anonymous_read_only_public_read_collections(kind):
    for principal in ("outsider", "anonymous"):
        assert can_read(KINDS[kind], principal) is (kind in PUBLIC_KINDS)


# --------------------------------------------------------------------------
# Record validation
# --------------------------------------------------------------------------


def _notes_spec() -> CollectionSpec:
    return CollectionSpec.model_validate(
        {
            "scope": "shared",
            "create": "members",
            "modify": "author",
            "fields": {
                "country": {"type": "string", "required": True},
                "text": {"type": "string", "required": True, "max_length": 2000},
                "priority": {"type": "string", "default": "normal"},
                "score": {"type": "number"},
                "done": {"type": "boolean"},
                "due": {"type": "date"},
                "snapshot": {"type": "json"},
            },
        }
    )


def _valid() -> dict:
    return {"country": "FR", "text": "hello"}


def _assert_rejected(fn, *args, code="validation", field=None):
    with pytest.raises(RecordValidationError) as exc_info:
        fn(*args)
    err = exc_info.value
    assert err.code == code
    assert err.reason
    if field is not None:
        assert err.field == field
    return err


class TestValidateNewRecord:
    def test_valid_record_returned_as_given_without_defaults(self):
        data = {**_valid(), "score": 3}
        result = validate_new_record(_notes_spec(), data)
        # Defaults are applied at read time, never written into the row.
        assert result == {"country": "FR", "text": "hello", "score": 3}
        assert "priority" not in result

    def test_missing_required_without_default_rejected(self):
        _assert_rejected(validate_new_record, _notes_spec(), {"country": "FR"}, field="text")

    def test_missing_required_with_default_accepted(self):
        spec = CollectionSpec.model_validate(
            {"scope": "per_user", "fields": {"level": {"type": "string", "required": True, "default": "low"}}}
        )
        assert validate_new_record(spec, {}) == {}

    def test_null_for_required_field_rejected(self):
        _assert_rejected(validate_new_record, _notes_spec(), {"country": None, "text": "x"}, field="country")

    @pytest.mark.parametrize("field", ["priority", "score", "done", "due", "snapshot"])
    def test_null_for_optional_field_accepted(self, field):
        data = {**_valid(), field: None}
        assert validate_new_record(_notes_spec(), data)[field] is None

    @pytest.mark.parametrize("key", ["unknown", "user_id", "mine", "version", "id"])
    def test_undeclared_keys_rejected_including_envelope_names(self, key):
        _assert_rejected(validate_new_record, _notes_spec(), {**_valid(), key: "x"}, field=key)

    def test_envelope_name_is_plain_app_field_when_declared(self):
        spec = CollectionSpec.model_validate({"scope": "per_user", "fields": {"version": {"type": "string"}}})
        assert validate_new_record(spec, {"version": "v2"}) == {"version": "v2"}

    @pytest.mark.parametrize(
        "field,value",
        [
            ("text", "plain"),
            ("text", ""),
            ("score", 0),
            ("score", -12),
            ("score", 3.25),
            ("done", True),
            ("done", False),
            ("due", "2026-09-27"),
            ("due", "2026-09-27T10:15:00"),
            ("due", "2026-09-27T10:15:00+02:00"),
            ("snapshot", {"rows": [[1, "a"], [2, "b"]]}),
            ("snapshot", [1, 2, 3]),
            ("snapshot", "text"),
            ("snapshot", 5),
            ("snapshot", True),
        ],
    )
    def test_values_of_declared_type_accepted(self, field, value):
        data = {**_valid(), field: value}
        assert validate_new_record(_notes_spec(), data)[field] == value

    @pytest.mark.parametrize(
        "field,value",
        [
            ("text", 5),
            ("text", ["a"]),
            ("score", "3"),
            ("score", True),
            ("score", False),
            ("score", float("nan")),
            ("score", float("inf")),
            ("score", float("-inf")),
            ("score", {"v": 1}),
            ("done", 1),
            ("done", "true"),
            ("due", "tomorrow"),
            ("due", "2026-13-01"),
            ("due", 20260927),
            ("due", ""),
            # Beyond float range: math.isfinite would raise OverflowError.
            ("score", 10**400),
            ("score", -(10**400)),
        ],
    )
    def test_values_of_wrong_type_rejected(self, field, value):
        _assert_rejected(validate_new_record, _notes_spec(), {**_valid(), field: value}, field=field)

    def test_huge_integer_from_json_body_rejected(self):
        # What the route receives for a 400-digit JSON number literal.
        data = json.loads('{"country": "FR", "text": "x", "score": 1' + "0" * 400 + "}")
        assert isinstance(data["score"], int)
        _assert_rejected(validate_new_record, _notes_spec(), data, field="score")

    def test_max_length_counts_characters_not_bytes(self):
        # "é" is two bytes in UTF-8; 2000 of them is still 2000 characters.
        ok = {**_valid(), "text": "é" * 2000}
        assert validate_new_record(_notes_spec(), ok)["text"] == "é" * 2000
        _assert_rejected(validate_new_record, _notes_spec(), {**_valid(), "text": "é" * 2001}, field="text")

    def test_non_object_data_rejected(self):
        _assert_rejected(validate_new_record, _notes_spec(), ["country", "FR"])

    def test_input_is_not_mutated(self):
        data = _valid()
        validate_new_record(_notes_spec(), data)
        assert data == {"country": "FR", "text": "hello"}


def _big_spec() -> CollectionSpec:
    return CollectionSpec.model_validate(
        {
            "scope": "per_user",
            "fields": {
                "text": {"type": "string"},
                "note": {"type": "string"},
                "blob": {"type": "json"},
                "blob2": {"type": "json"},
            },
        }
    )


class TestSizeLimits:
    # '{"text":""}' is 11 bytes of compact JSON around the string value.
    TEXT_OVERHEAD = 11

    def test_record_exactly_at_limit_accepted(self):
        data = {"text": "x" * (MAX_RECORD_BYTES - self.TEXT_OVERHEAD)}
        assert validate_new_record(_big_spec(), data) == data

    def test_record_one_byte_over_limit_rejected(self):
        data = {"text": "x" * (MAX_RECORD_BYTES - self.TEXT_OVERHEAD + 1)}
        _assert_rejected(validate_new_record, _big_spec(), data, code="too_large")

    def test_limit_counts_utf8_bytes(self):
        # 32,763 two-byte characters = 65,526 bytes + 11 overhead = 65,537 > 65,536.
        data = {"text": "é" * 32_763}
        _assert_rejected(validate_new_record, _big_spec(), data, code="too_large")
        data_ok = {"text": "é" * 32_762}
        assert validate_new_record(_big_spec(), data_ok) == data_ok

    def test_json_fields_are_exempt_from_the_record_limit(self):
        data = {"blob": {"rows": "x" * 200_000}}
        assert validate_new_record(_big_spec(), data) == data

    def test_single_json_field_over_its_limit_rejected(self):
        data = {"blob": "x" * MAX_JSON_FIELD_BYTES}
        err = _assert_rejected(validate_new_record, _big_spec(), data, code="too_large")
        assert err.field == "blob"

    def test_whole_record_over_total_limit_rejected(self):
        half = "x" * (MAX_RECORD_TOTAL_BYTES // 2)
        data = {"blob": half, "blob2": half}
        _assert_rejected(validate_new_record, _big_spec(), data, code="too_large")

    def test_limit_applies_to_the_projected_record_with_defaults(self):
        # Codex #6: what a read returns (stored values + defaults for missing
        # fields) must fit the record limit too.
        spec = CollectionSpec.model_validate({"scope": "per_user", "fields": {
            "text": {"type": "string"},
            "note": {"type": "string", "default": "d" * 40_000},
        }})
        _assert_rejected(validate_new_record, spec, {"text": "x" * 30_000}, code="too_large")
        assert validate_new_record(spec, {"text": "x" * 30_000, "note": ""}) == {"text": "x" * 30_000, "note": ""}
        _assert_rejected(validate_record_patch, spec, {"text": "short"}, {"text": "x" * 30_000}, code="too_large")

    def test_patch_limit_applies_to_merged_record(self):
        stored = {"text": "x" * 40_000}
        patch = {"note": "y" * 40_000}
        _assert_rejected(validate_record_patch, _big_spec(), stored, patch, code="too_large")


class TestValidateRecordPatch:
    def test_shallow_merge(self):
        stored = {"country": "FR", "text": "a", "score": 1}
        assert validate_record_patch(_notes_spec(), stored, {"text": "b"}) == {
            "country": "FR",
            "text": "b",
            "score": 1,
        }

    def test_empty_patch_is_valid_noop(self):
        stored = {"country": "FR", "text": "a"}
        assert validate_record_patch(_notes_spec(), stored, {}) == stored

    def test_undeclared_patch_key_rejected(self):
        _assert_rejected(validate_record_patch, _notes_spec(), _valid(), {"mine": True}, field="mine")

    def test_patch_values_type_checked(self):
        _assert_rejected(validate_record_patch, _notes_spec(), _valid(), {"score": True}, field="score")
        _assert_rejected(validate_record_patch, _notes_spec(), _valid(), {"score": float("nan")}, field="score")
        _assert_rejected(validate_record_patch, _notes_spec(), _valid(), {"score": 10**400}, field="score")

    def test_patch_cannot_null_a_required_field(self):
        _assert_rejected(validate_record_patch, _notes_spec(), _valid(), {"text": None}, field="text")

    def test_patch_can_null_an_optional_field(self):
        stored = {**_valid(), "score": 4}
        assert validate_record_patch(_notes_spec(), stored, {"score": None})["score"] is None

    def test_removed_field_value_preserved_on_merge(self):
        stored = {**_valid(), "legacy": "keep me"}
        merged = validate_record_patch(_notes_spec(), stored, {"text": "b"})
        assert merged["legacy"] == "keep me"
        assert "legacy" not in project_record_data(_notes_spec(), merged)

    def test_required_field_added_later_with_default_satisfied(self):
        spec = CollectionSpec.model_validate(
            {
                "scope": "per_user",
                "fields": {
                    "text": {"type": "string"},
                    "status": {"type": "string", "required": True, "default": "open"},
                },
            }
        )
        stored = {"text": "old row"}
        assert validate_record_patch(spec, stored, {"text": "new"}) == {"text": "new"}
        assert project_record_data(spec, stored) == {"text": "old row", "status": "open"}

    def test_required_field_added_later_without_default_blocks_patch(self):
        spec = CollectionSpec.model_validate(
            {
                "scope": "per_user",
                "fields": {"text": {"type": "string"}, "status": {"type": "string", "required": True}},
            }
        )
        _assert_rejected(validate_record_patch, spec, {"text": "old"}, {"text": "new"}, field="status")
        assert validate_record_patch(spec, {"text": "old"}, {"status": "set"}) == {"text": "old", "status": "set"}

    def test_type_changed_field_left_alone_when_not_patched(self):
        stored = {**_valid(), "score": "three"}  # written when score was a string
        merged = validate_record_patch(_notes_spec(), stored, {"text": "b"})
        assert merged["score"] == "three"
        assert project_record_data(_notes_spec(), merged)["score"] == "three"
        _assert_rejected(validate_record_patch, _notes_spec(), stored, {"score": "four"}, field="score")

    def test_inputs_not_mutated(self):
        stored = _valid()
        patch = {"text": "b"}
        validate_record_patch(_notes_spec(), stored, patch)
        assert stored == {"country": "FR", "text": "hello"}
        assert patch == {"text": "b"}


class TestProjectRecordData:
    def test_missing_fields_filled_from_defaults_including_null(self):
        spec = CollectionSpec.model_validate(
            {
                "scope": "per_user",
                "fields": {
                    "genre": {"type": "string", "default": None},
                    "priority": {"type": "string", "default": "normal"},
                    "note": {"type": "string"},
                },
            }
        )
        assert project_record_data(spec, {}) == {"genre": None, "priority": "normal"}

    def test_stored_values_win_over_defaults_even_null(self):
        spec = CollectionSpec.model_validate(
            {"scope": "per_user", "fields": {"priority": {"type": "string", "default": "normal"}}}
        )
        assert project_record_data(spec, {"priority": None}) == {"priority": None}
        assert project_record_data(spec, {"priority": "high"}) == {"priority": "high"}

    def test_removed_fields_hidden_and_restored_on_revert(self):
        stored = {**_valid(), "legacy": 1}
        assert "legacy" not in project_record_data(_notes_spec(), stored)
        reverted = CollectionSpec.model_validate(
            {"scope": "per_user", "fields": {"legacy": {"type": "number"}, "text": {"type": "string"}}}
        )
        assert project_record_data(reverted, stored) == {"legacy": 1, "text": "hello"}

    def test_json_default_is_not_shared_between_records(self):
        spec = CollectionSpec.model_validate(
            {"scope": "per_user", "fields": {"tags": {"type": "json", "default": []}}}
        )
        first = project_record_data(spec, {})
        first["tags"].append("x")
        assert project_record_data(spec, {}) == {"tags": []}

    def test_stored_is_not_mutated(self):
        stored = {"country": "FR"}
        project_record_data(_notes_spec(), stored)
        assert stored == {"country": "FR"}

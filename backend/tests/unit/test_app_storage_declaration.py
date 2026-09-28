"""Contract tests for the artifact app storage declaration (schemas/app_storage.py).

The declaration lives in ``ArtifactVersion.content.storage``. These tests pin
what a valid declaration is (RD8 strictness), how defaults are recognized, and
the shape of the record DTOs the API will speak.
"""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.app_storage import (
    COLLECTION_NAME_PATTERN,
    FIELD_NAME_PATTERN,
    MAX_COLLECTIONS,
    MAX_FIELDS_PER_COLLECTION,
    MAX_JSON_FIELD_BYTES,
    MAX_RECORD_BYTES,
    MAX_RECORD_TOTAL_BYTES,
    MAX_RECORDS_PER_COLLECTION,
    AppRecordCreate,
    AppRecordDelete,
    AppRecordDeleted,
    AppRecordList,
    AppRecordOut,
    AppRecordUpdate,
    AppRecordUser,
    CollectionSpec,
    FieldSpec,
    StorageDeclaration,
    parse_storage_declaration,
)


def _spec_example() -> dict:
    """The declaration example from the design spec, section 6."""
    return {
        "collections": {
            "notes": {
                "scope": "shared",
                "create": "members",
                "modify": "author",
                "fields": {
                    "country": {"type": "string", "required": True},
                    "text": {"type": "string", "required": True, "max_length": 2000},
                    "priority": {"type": "string", "default": "normal"},
                },
            },
            "selections": {
                "scope": "per_user",
                "fields": {"genre": {"type": "string", "default": None}},
            },
        }
    }


def _collection(**overrides) -> dict:
    base = {"scope": "shared", "create": "members", "modify": "author", "fields": {"a": {"type": "string"}}}
    base.update(overrides)
    return base


class TestLimits:
    def test_limit_constants_match_agreed_values(self):
        assert MAX_COLLECTIONS == 20
        assert MAX_FIELDS_PER_COLLECTION == 50
        assert MAX_RECORD_BYTES == 65_536
        assert MAX_JSON_FIELD_BYTES == 262_144
        assert MAX_RECORD_TOTAL_BYTES == 262_144
        assert MAX_RECORDS_PER_COLLECTION == 10_000
        assert COLLECTION_NAME_PATTERN == r"^[a-z][a-z0-9_]{0,63}$"
        assert FIELD_NAME_PATTERN == r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"


class TestParse:
    def test_none_means_no_storage(self):
        assert parse_storage_declaration(None) is None

    def test_spec_example_parses(self):
        decl = parse_storage_declaration(_spec_example())
        assert isinstance(decl, StorageDeclaration)
        assert set(decl.collections) == {"notes", "selections"}
        notes = decl.collections["notes"]
        assert notes.scope == "shared" and notes.create == "members" and notes.modify == "author"
        assert notes.fields["text"].max_length == 2000
        assert notes.fields["country"].required is True
        assert decl.collections["selections"].scope == "per_user"

    def test_empty_collections_is_valid(self):
        decl = parse_storage_declaration({"collections": {}})
        assert decl is not None and decl.collections == {}

    @pytest.mark.parametrize("raw", ["storage", 3, [], {"collections": []}, {}])
    def test_non_declaration_shapes_rejected(self, raw):
        with pytest.raises(ValidationError):
            parse_storage_declaration(raw)

    def test_unknown_top_level_key_rejected(self):
        raw = _spec_example()
        raw["public_read"] = True
        with pytest.raises(ValidationError):
            parse_storage_declaration(raw)


class TestCollectionNames:
    @pytest.mark.parametrize("name", ["notes", "a", "slides_v2", "x" + "y" * 63])
    def test_valid_names(self, name):
        decl = parse_storage_declaration({"collections": {name: _collection()}})
        assert name in decl.collections

    @pytest.mark.parametrize(
        "name", ["Notes", "1notes", "_notes", "no-tes", "no tes", "../x", "", "x" + "y" * 64, "notes\n"]
    )
    def test_invalid_names_rejected(self, name):
        with pytest.raises(ValidationError):
            parse_storage_declaration({"collections": {name: _collection()}})

    def test_collection_count_bounds(self):
        ok = {f"c{i}": _collection() for i in range(MAX_COLLECTIONS)}
        assert len(parse_storage_declaration({"collections": ok}).collections) == MAX_COLLECTIONS
        too_many = {f"c{i}": _collection() for i in range(MAX_COLLECTIONS + 1)}
        with pytest.raises(ValidationError):
            parse_storage_declaration({"collections": too_many})


class TestCollectionSpec:
    def test_scope_required(self):
        with pytest.raises(ValidationError):
            CollectionSpec.model_validate({"fields": {"a": {"type": "string"}}})

    @pytest.mark.parametrize("missing", ["create", "modify"])
    def test_shared_requires_create_and_modify(self, missing):
        raw = _collection()
        raw.pop(missing)
        with pytest.raises(ValidationError):
            CollectionSpec.model_validate(raw)

    def test_per_user_does_not_need_create_or_modify(self):
        spec = CollectionSpec.model_validate({"scope": "per_user", "fields": {"a": {"type": "string"}}})
        assert spec.scope == "per_user"

    @pytest.mark.parametrize("rules", [
        {"create": "members", "modify": "members"},  # invalid modify value, meaningless for per_user
        {"create": "members", "modify": "owner"},    # already-stored shape
        {"create": "anyone"},
    ])
    def test_per_user_create_and_modify_are_dropped_not_rejected(self, rules):
        spec = CollectionSpec.model_validate({"scope": "per_user", **rules, "fields": {"a": {"type": "string"}}})
        assert (spec.create, spec.modify) == (None, None)
        assert spec.model_dump(mode="json", exclude_unset=True) == {"scope": "per_user", "fields": {"a": {"type": "string"}}}

    def test_stored_per_user_declaration_with_rules_still_parses(self):
        raw = {"collections": {"prefs": {"scope": "per_user", "create": "members", "modify": "owner",
                                         "fields": {"genre": {"type": "string"}}}}}
        decl = parse_storage_declaration(raw)
        assert decl.collections["prefs"].scope == "per_user"

    def test_per_user_still_rejects_unknown_keys(self):
        with pytest.raises(ValidationError):
            CollectionSpec.model_validate({"scope": "per_user", "public": True, "fields": {"a": {"type": "string"}}})

    @pytest.mark.parametrize(
        "overrides",
        [
            {"scope": "public"},
            {"create": "anyone"},
            {"modify": "members"},
            {"public_read": True},
        ],
    )
    def test_unknown_rule_values_and_keys_rejected(self, overrides):
        with pytest.raises(ValidationError):
            CollectionSpec.model_validate(_collection(**overrides))

    def test_fields_required_nonempty(self):
        with pytest.raises(ValidationError):
            CollectionSpec.model_validate(_collection(fields={}))
        with pytest.raises(ValidationError):
            CollectionSpec.model_validate({"scope": "per_user"})

    def test_field_count_bounds(self):
        ok = {f"f{i}": {"type": "string"} for i in range(MAX_FIELDS_PER_COLLECTION)}
        assert len(CollectionSpec.model_validate(_collection(fields=ok)).fields) == MAX_FIELDS_PER_COLLECTION
        too_many = {f"f{i}": {"type": "string"} for i in range(MAX_FIELDS_PER_COLLECTION + 1)}
        with pytest.raises(ValidationError):
            CollectionSpec.model_validate(_collection(fields=too_many))

    @pytest.mark.parametrize("name", ["a", "Country", "_private", "camelCase9", "A" * 64])
    def test_valid_field_names(self, name):
        spec = CollectionSpec.model_validate(_collection(fields={name: {"type": "string"}}))
        assert name in spec.fields

    @pytest.mark.parametrize("name", ["9a", "a-b", "a b", "", "a.b", "A" * 65, "a\n"])
    def test_invalid_field_names_rejected(self, name):
        with pytest.raises(ValidationError):
            CollectionSpec.model_validate(_collection(fields={name: {"type": "string"}}))


class TestFieldSpec:
    @pytest.mark.parametrize("ftype", ["string", "number", "boolean", "date", "json"])
    def test_every_field_type_accepted(self, ftype):
        assert FieldSpec.model_validate({"type": ftype}).type == ftype

    @pytest.mark.parametrize("ftype", ["text", "int", "object", "", None])
    def test_unknown_field_type_rejected(self, ftype):
        with pytest.raises(ValidationError):
            FieldSpec.model_validate({"type": ftype})

    def test_unknown_field_key_rejected(self):
        with pytest.raises(ValidationError):
            FieldSpec.model_validate({"type": "string", "unique": True})

    def test_has_default_only_when_explicit(self):
        assert FieldSpec.model_validate({"type": "string"}).has_default is False
        assert FieldSpec.model_validate({"type": "string", "default": None}).has_default is True
        assert FieldSpec.model_validate({"type": "string", "default": "normal"}).has_default is True
        assert FieldSpec.model_validate({"type": "boolean", "default": False}).has_default is True

    @pytest.mark.parametrize(
        "ftype,default",
        [
            ("string", "x"),
            ("string", ""),
            ("number", 0),
            ("number", 2.5),
            ("boolean", True),
            ("date", "2026-09-27"),
            ("date", "2026-09-27T10:00:00"),
            ("json", {"a": [1, 2]}),
            ("json", [1]),
            ("json", "s"),
            ("string", None),
            ("number", None),
        ],
    )
    def test_default_matching_type_accepted(self, ftype, default):
        assert FieldSpec.model_validate({"type": ftype, "default": default}).default == default

    @pytest.mark.parametrize(
        "ftype,default",
        [
            ("string", 1),
            ("number", "1"),
            ("number", True),
            ("number", float("nan")),
            ("boolean", 0),
            ("boolean", "true"),
            ("date", "yesterday"),
            ("date", 20260927),
            # Beyond float range: must be a plain rejection, not an OverflowError.
            ("number", 10**400),
            ("number", -(10**400)),
        ],
    )
    def test_default_not_matching_type_rejected(self, ftype, default):
        with pytest.raises(ValidationError):
            FieldSpec.model_validate({"type": ftype, "default": default})

    def test_huge_integer_default_rejected_through_parse(self):
        raw = {"collections": {"scores": {"scope": "per_user", "fields": {"n": {"type": "number", "default": 10**400}}}}}
        with pytest.raises(ValidationError):
            parse_storage_declaration(raw)

    @pytest.mark.parametrize("ftype", ["string", "number", "boolean", "date", "json"])
    def test_required_with_explicit_null_default_rejected(self, ftype):
        # A null default would satisfy "required" for every record, making it meaningless.
        with pytest.raises(ValidationError):
            FieldSpec.model_validate({"type": ftype, "required": True, "default": None})

    def test_required_with_non_null_default_accepted(self):
        spec = FieldSpec.model_validate({"type": "string", "required": True, "default": "low"})
        assert spec.required is True and spec.has_default is True

    def test_max_length_only_for_string(self):
        assert FieldSpec.model_validate({"type": "string", "max_length": 10}).max_length == 10
        with pytest.raises(ValidationError):
            FieldSpec.model_validate({"type": "number", "max_length": 10})

    @pytest.mark.parametrize("max_length", [0, -1, 100_001])
    def test_max_length_bounds(self, max_length):
        with pytest.raises(ValidationError):
            FieldSpec.model_validate({"type": "string", "max_length": max_length})

    def test_string_default_longer_than_max_length_rejected(self):
        with pytest.raises(ValidationError):
            FieldSpec.model_validate({"type": "string", "max_length": 3, "default": "abcd"})


class TestPublicRead:
    """Reading through a public link is its own decision (spec 7, revised):
    ``public_read`` is allowed only on shared collections the owner alone writes."""

    def test_defaults_to_false_and_stays_out_of_the_stored_shape(self):
        spec = CollectionSpec.model_validate(_collection(create="owner", modify="owner"))
        assert spec.public_read is False
        assert "public_read" not in spec.model_dump(mode="json", exclude_unset=True)

    def test_stored_declaration_without_it_parses_as_not_public(self):
        decl = parse_storage_declaration(_spec_example())
        assert all(c.public_read is False for c in decl.collections.values())

    @pytest.mark.parametrize("modify", ["owner", "author"])
    def test_allowed_on_shared_owner_created(self, modify):
        spec = CollectionSpec.model_validate(_collection(create="owner", modify=modify, public_read=True))
        assert spec.public_read is True
        assert spec.model_dump(mode="json", exclude_unset=True)["public_read"] is True

    def test_rejected_on_shared_members_created(self):
        with pytest.raises(ValidationError) as exc_info:
            CollectionSpec.model_validate(_collection(create="members", public_read=True))
        assert "public_read" in str(exc_info.value) and "owner" in str(exc_info.value)

    def test_rejected_on_per_user(self):
        # per_user drops create/modify, but never public_read.
        with pytest.raises(ValidationError) as exc_info:
            CollectionSpec.model_validate({"scope": "per_user", "public_read": True, "fields": {"a": {"type": "string"}}})
        assert "public_read" in str(exc_info.value)

    def test_explicit_false_is_accepted_everywhere(self):
        assert CollectionSpec.model_validate(_collection(public_read=False)).public_read is False
        spec = CollectionSpec.model_validate({"scope": "per_user", "public_read": False, "fields": {"a": {"type": "string"}}})
        assert spec.public_read is False

    def test_must_be_a_boolean(self):
        with pytest.raises(ValidationError):
            CollectionSpec.model_validate(_collection(create="owner", modify="owner", public_read="yes"))


class TestDefaultSizes:
    """Codex #6: a default is returned with every record that lacks the field,
    so it is held to the same byte limits as stored values."""

    def test_json_default_over_the_json_field_limit_rejected(self):
        # ~300 KB: one default alone would push every record over the limit.
        with pytest.raises(ValidationError) as exc_info:
            FieldSpec.model_validate({"type": "json", "default": {"blob": "x" * 300_000}})
        assert "default" in str(exc_info.value)

    def test_json_default_at_the_json_field_limit_accepted(self):
        # '"' + value + '"' is the compact JSON of a string.
        value = "x" * (MAX_JSON_FIELD_BYTES - 2)
        assert FieldSpec.model_validate({"type": "json", "default": value}).default == value

    def test_string_default_over_the_record_limit_rejected(self):
        with pytest.raises(ValidationError):
            FieldSpec.model_validate({"type": "string", "default": "x" * MAX_RECORD_BYTES})

    def test_non_json_defaults_together_over_the_record_limit_rejected(self):
        half = "x" * (MAX_RECORD_BYTES // 2)
        fields = {"a": {"type": "string", "default": half}, "b": {"type": "string", "default": half}}
        FieldSpec.model_validate(fields["a"])  # each alone is fine
        with pytest.raises(ValidationError) as exc_info:
            CollectionSpec.model_validate({"scope": "per_user", "fields": fields})
        assert "default" in str(exc_info.value)

    def test_json_defaults_together_over_the_total_limit_rejected(self):
        big = "x" * (MAX_JSON_FIELD_BYTES - 100)
        fields = {"a": {"type": "json", "default": big}, "b": {"type": "json", "default": big}}
        with pytest.raises(ValidationError):
            CollectionSpec.model_validate({"scope": "per_user", "fields": fields})

    def test_300kb_default_rejected_through_parse(self):
        raw = {"collections": {"slides": {"scope": "shared", "create": "owner", "modify": "owner",
                                          "fields": {"snapshot": {"type": "json", "default": ["x" * 300_000]}}}}}
        with pytest.raises(ValidationError):
            parse_storage_declaration(raw)

    def test_small_defaults_accepted(self):
        spec = CollectionSpec.model_validate({"scope": "per_user", "fields": {
            "tags": {"type": "json", "default": []}, "level": {"type": "string", "default": "low"},
        }})
        assert spec.fields["tags"].default == []


class TestDtos:
    def test_update_and_delete_require_positive_version(self):
        assert AppRecordUpdate.model_validate({"data": {}, "version": 1}).version == 1
        assert AppRecordDelete.model_validate({"version": 7}).version == 7
        for bad in (0, -1):
            with pytest.raises(ValidationError):
                AppRecordUpdate.model_validate({"data": {}, "version": bad})
            with pytest.raises(ValidationError):
                AppRecordDelete.model_validate({"version": bad})
        with pytest.raises(ValidationError):
            AppRecordUpdate.model_validate({"data": {}})
        with pytest.raises(ValidationError):
            AppRecordDelete.model_validate({})

    def test_create_requires_object_data(self):
        assert AppRecordCreate.model_validate({"data": {"a": 1}}).data == {"a": 1}
        with pytest.raises(ValidationError):
            AppRecordCreate.model_validate({"data": [1]})
        with pytest.raises(ValidationError):
            AppRecordCreate.model_validate({})

    def test_record_out_and_list_shape(self):
        now = datetime(2026, 9, 27, tzinfo=timezone.utc)
        out = AppRecordOut(
            id="r1",
            data={"a": 1},
            user=AppRecordUser(id="u1", name="Ann"),
            version=1,
            created_at=now,
            updated_at=now,
            mine=True,
        )
        dumped = AppRecordList(items=[out]).model_dump()
        item = dumped["items"][0]
        assert set(item) == {"id", "data", "user", "version", "created_at", "updated_at", "mine"}
        assert item["user"] == {"id": "u1", "name": "Ann"}
        anonymous_author = AppRecordOut(
            id="r2", data={}, user=None, version=1, created_at=now, updated_at=now, mine=False
        )
        assert anonymous_author.user is None
        assert AppRecordUser(id="u2").name is None

    def test_deleted_shape(self):
        assert AppRecordDeleted(id="r1").model_dump() == {"id": "r1", "deleted": True}

import pytest
from pydantic import ValidationError
from app.schemas.artifact_resource_schema import ResourceDefinition, validate_record


def definition(**changes):
    value = {
        "name": "entries",
        "kind": "collection",
        "fields": {
            "title": {"type": "string", "required": True, "max_length": 80},
            "status": {"type": "string", "enum": ["draft", "published"], "default": "draft", "indexed": True},
            "score": {"type": "number"},
        },
    }
    return ResourceDefinition.model_validate(value | changes)


def test_defaults_and_validation_are_server_owned():
    schema = definition()
    assert validate_record(schema, {"title": "One"}) == {"title": "One", "status": "draft"}
    for payload in ({}, {"title": "x", "extra": 1}, {"title": "x", "score": True}, {"title": "x", "status": "hidden"}):
        with pytest.raises(ValueError):
            validate_record(schema, payload)


def test_resource_contract_rejects_unknown_or_unsafe_declarations():
    for changes in ({"name": "../entries"}, {"kind": "sql"}, {"secret": "x"}, {"fields": {"id": {"type": "string"}}}):
        with pytest.raises(ValidationError):
            definition(**changes)


def test_defaults_do_not_mutate_input_and_required_fields_cannot_be_null():
    payload = {"title": "Two"}
    validate_record(definition(), payload)
    assert payload == {"title": "Two"}
    with pytest.raises(ValueError):
        validate_record(definition(), {"title": None})


def test_permission_schema_is_flat_and_rejects_nested_alternatives():
    from app.schemas.artifact_resource_schema import Rule, RuleScope

    assert "any_of" not in RuleScope.model_json_schema()["properties"]
    allowed = Rule.model_validate({"any_of": [{"audience": "owner"}, {"audience": "public", "any_of": []}]})
    assert len(allowed.any_of) == 2
    with pytest.raises(ValidationError):
        Rule.model_validate({"any_of": [{"any_of": [{"audience": "public"}]}]})

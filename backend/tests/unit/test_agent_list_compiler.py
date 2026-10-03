"""Contracts for the Agent List schema compiler.

For every combination of field type × required/optional the compiled
submit_<list> input schema must:
- be a valid Draft 2020-12 schema;
- stay inside the strict subset providers accept (additionalProperties:false
  and required == all property keys on every object; no numeric/length/pattern
  keywords);
- be deterministic (byte-identical across compiles — the prompt-cache invariant);
- accept a well-formed record and reject wrong types / missing / extra keys
  with a path-qualified error.
"""
import itertools
import json

import pytest
from jsonschema import Draft202012Validator

from app.ai.tools.mcp_schema import validate_arguments
from app.schemas.agent_list import ListFieldDef
from app.services.agent_lists.compiler import compile_list_schema

TYPES = ["string", "number", "integer", "boolean", "date", "enum"]
FORBIDDEN = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
             "minLength", "maxLength", "pattern", "if", "then", "else", "$ref"}

SAMPLE = {"string": "Acme", "number": 12.5, "integer": 3, "boolean": True, "date": "2026-01-31", "enum": "B"}
WRONG = {"string": 5, "number": "12", "integer": "x", "boolean": "yes", "date": 20260131, "enum": 7}


def _field(t, required, i=0):
    return ListFieldDef(name=f"f_{t}_{i}", type=t, required=required,
                        enum=["A", "B"] if t == "enum" else None, description=f"{t} field").model_dump()


def _all_objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object" or (isinstance(node.get("type"), list) and "object" in node["type"]):
            yield node
        for v in node.values():
            yield from _all_objects(v)
    elif isinstance(node, list):
        for v in node:
            yield from _all_objects(v)


def _all_keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _all_keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _all_keys(v)


FIELD_SETS = [[_field(t, r)] for t, r in itertools.product(TYPES, [True, False])] + [
    [_field(t, i % 2 == 0, i) for i, t in enumerate(TYPES)]
]


@pytest.mark.parametrize("fields", FIELD_SETS)
def test_compiled_schema_is_strict_subset_and_valid(fields):
    schema = compile_list_schema(fields, "desc")
    Draft202012Validator.check_schema(schema)
    for obj in _all_objects(schema):
        assert obj.get("additionalProperties") is False
        assert sorted(obj.get("required", [])) == sorted(obj.get("properties", {}).keys())
    assert not (set(_all_keys(schema)) & FORBIDDEN)


@pytest.mark.parametrize("fields", FIELD_SETS)
def test_compile_is_deterministic(fields):
    a = json.dumps(compile_list_schema(fields, "d"), sort_keys=False)
    b = json.dumps(compile_list_schema([dict(f) for f in fields], "d"), sort_keys=False)
    assert a == b


def _env(value, status="found"):
    return {"value": value, "status": status, "evidence": [], "note": None}


@pytest.mark.parametrize("t", TYPES)
def test_round_trip_valid_and_invalid(t):
    f = _field(t, True)
    schema = compile_list_schema([f])
    good = {"records": [{"row_id": None, "fields": {f["name"]: _env(SAMPLE[t])}}]}
    ok, errs = validate_arguments(good, schema)
    assert ok, errs

    bad_type = {"records": [{"row_id": None, "fields": {f["name"]: _env(WRONG[t])}}]}
    ok, errs = validate_arguments(bad_type, schema)
    assert not ok and any(e.startswith(f"records.0.fields.{f['name']}") for e in errs), errs

    missing = {"records": [{"row_id": None, "fields": {}}]}
    ok, errs = validate_arguments(missing, schema)
    assert not ok and any(e.startswith("records.0.fields") for e in errs), errs

    extra = {"records": [{"row_id": None, "fields": {f["name"]: _env(SAMPLE[t]), "nope": None}}]}
    ok, errs = validate_arguments(extra, schema)
    assert not ok and any(e.startswith("records.0.fields") for e in errs), errs


def test_null_field_envelope_is_allowed_for_partial_updates():
    f = _field("string", False)
    schema = compile_list_schema([f])
    ok, errs = validate_arguments({"records": [{"row_id": "r1", "fields": {f["name"]: None}}]}, schema)
    assert ok, errs

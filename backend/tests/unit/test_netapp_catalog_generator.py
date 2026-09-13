"""Swagger parameter inheritance is a contract of the generated catalog."""

import runpy
from pathlib import Path

import pytest


def generate(spec):
    return runpy.run_path(str(Path(__file__).parents[3] / "tools/netapp/generate_catalog.py"))["generate"](spec)


def specification():
    return {
        "definitions": {},
        "parameters": {
            "parent": {"in": "path", "name": "id", "type": "integer", "minimum": 0},
            "projection": {"in": "query", "name": "fields", "type": "array"},
        },
        "paths": {
            "/example/{id}": {
                "parameters": [
                    {"$ref": "#/parameters/parent"},
                    {"$ref": "#/parameters/projection"},
                    {"in": "query", "name": "name", "type": "string", "enum": ["old"]},
                ],
                "get": {
                    "parameters": [{"in": "query", "name": "name", "type": "string", "enum": ["new"]}],
                    "responses": {"200": {"schema": {"type": "object", "properties": {"name": {"type": "string"}}}}},
                },
            }
        },
    }


def test_inherited_referenced_parameters_and_operation_overrides():
    result = generate(specification())
    resource = next(iter(result["resources"].values()))
    assert resource["parents"] == ["id"]
    assert resource["parent_types"]["id"]["type"] == "integer"
    assert resource["controls"] == ["fields"]
    assert resource["filters"]["name"]["enum"] == ["new"]


def test_generator_rejects_undeclared_path_parameters():
    spec = specification()
    spec["paths"]["/example/{id}"]["parameters"].pop(0)
    with pytest.raises(ValueError, match="path parameters"):
        generate(spec)


def test_generator_rejects_history_without_time_contract():
    spec = specification()
    spec["paths"]["/example/{id}/metrics"] = spec["paths"].pop("/example/{id}")
    with pytest.raises(ValueError, match="time contract"):
        generate(spec)

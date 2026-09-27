"""The planner's storage authoring reference (Phase 9).

The planner authors artifact code itself, so everything it needs to declare
and use record storage must be in its reference: the runtime API (in
SANDBOX_RUNTIME_PROMPT), the declaration contract and the rebuild rule (in
_STORAGE_CONTRACT), and the `storage` tool field descriptions. The examples in
the reference must pass the same gates the tools apply.
"""
import json
import re

import pytest

from app.ai.agents.planner import artifact_authoring
from app.ai.agents.planner.artifact_authoring import build_artifact_authoring_reference
from app.ai.tools.implementations._artifact_storage import storage_reference_errors
from app.ai.tools.implementations._sandbox_context import (
    SANDBOX_RUNTIME_OBSERVATION,
    SANDBOX_RUNTIME_PROMPT,
)
from app.ai.tools.schemas.create_artifact import CreateArtifactInput
from app.ai.tools.schemas.edit_artifact import EditArtifactInput
from app.schemas.app_storage import parse_storage_declaration

_EXAMPLE_HEAD = re.compile(r"^EXAMPLE (\w+)", re.M)
_STORAGE_LINE = re.compile(r"^storage = (\{.*\})$", re.M)


def _contract() -> str:
    return getattr(artifact_authoring, "_STORAGE_CONTRACT", "")


def _examples():
    """(name, declaration dict, code) for every EXAMPLE block in the contract."""
    text = _contract()
    heads = list(_EXAMPLE_HEAD.finditer(text))
    out = []
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        block = text[head.start():end]
        decl = _STORAGE_LINE.search(block)
        assert decl, f"example {head.group(1)} has no `storage = {{...}}` line"
        out.append((head.group(1), json.loads(decl.group(1)), block[decl.end():]))
    return out


def test_contract_is_part_of_the_planner_reference():
    contract = _contract()
    assert contract.strip(), "_STORAGE_CONTRACT is missing"
    assert contract in build_artifact_authoring_reference()


def test_contract_stays_short():
    assert len(_contract().strip().splitlines()) <= 110


def test_three_examples_notes_form_blog():
    names = [name for name, _, _ in _examples()]
    assert names == ["notes", "remembered_form", "blog"]


@pytest.mark.parametrize("index", [0, 1, 2])
def test_example_declaration_parses_and_backs_every_call(index):
    name, raw, code = _examples()[index]
    decl = parse_storage_declaration(raw)
    assert decl is not None and decl.collections, name
    assert "useCollection(" in code, name
    assert storage_reference_errors(code, decl) == [], name


@pytest.mark.parametrize("index", [0, 1, 2])
def test_example_handles_write_rejections_and_renders_error(index):
    name, _, code = _examples()[index]
    assert ".catch(" in code or "catch (" in code, name
    assert "error" in code and "loading" in code, name


def test_example_rules_match_their_purpose():
    ex = {name: raw["collections"] for name, raw, _ in _examples()}
    notes = ex["notes"]["notes"]
    assert (notes["scope"], notes["create"], notes["modify"]) == ("shared", "members", "author")
    assert all(c["scope"] == "per_user" for c in ex["remembered_form"].values())
    blog = ex["blog"]
    assert (blog["posts"]["scope"], blog["posts"]["create"]) == ("shared", "owner")
    assert (blog["comments"]["create"], blog["comments"]["modify"]) == ("members", "author")


def test_contract_names_the_vocabulary():
    contract = _contract()
    for token in ("useCollection(", "replaces_artifact_id", "storage_change_not_confirmed"):
        assert token in contract, token
    for field_type in ("string", "number", "boolean", "date", "json"):
        assert f"`{field_type}`" in contract, field_type
    for rule in ("shared", "per_user", "members", "owner", "author"):
        assert rule in contract, rule
    for kind in ("collection_removed", "field_removed", "field_type_changed",
                 "scope_changed", "create_changed", "field_made_required", "collection_readded"):
        assert kind in contract, kind


def test_contract_lists_every_change_kind_the_gate_reports():
    from typing import get_args

    from app.ai.tools.implementations._artifact_storage import StorageChange

    contract = _contract()
    for kind in get_args(StorageChange.model_fields["kind"].annotation):
        assert kind in contract, kind


def test_contract_states_the_direct_call_rule():
    # The gate rejects aliases, optional calls and bracket access.
    assert "directly" in _contract()


def test_runtime_prompt_teaches_app_data_after_viewer_identity():
    prompt = SANDBOX_RUNTIME_PROMPT
    assert "APP DATA" in prompt
    assert prompt.index("VIEWER IDENTITY") < prompt.index("APP DATA")
    section = prompt[prompt.index("APP DATA"):]
    for token in ("useCollection(", "items", "loading", "error", "add", "update", "remove",
                  "refresh", "version", "mine", "conflict", "AppDataError", ".catch("):
        assert token in section, token


def test_observation_mentions_use_collection():
    assert "useCollection(" in SANDBOX_RUNTIME_OBSERVATION


def test_storage_field_descriptions():
    create = CreateArtifactInput.model_fields["storage"].description or ""
    edit = EditArtifactInput.model_fields["storage"].description or ""
    assert "replaces_artifact_id" in create and "empty" in create.lower()
    assert "keep" in edit and "replaces" in edit
    for desc in (create, edit):
        assert "approv" in desc


@pytest.mark.parametrize("index", [0, 1, 2])
def test_example_passes_the_write_handling_check(index):
    from app.ai.tools.implementations._artifact_storage import write_handling_note

    name, _, code = _examples()[index]
    assert write_handling_note(code) == "", name


def _phrase_rows():
    """{phrase line: rules} for the '→' rows of the phrase table."""
    rows = {}
    for line in _contract().splitlines():
        if line.startswith("- \"") and "→" in line:
            phrase, rules = line.split("→", 1)
            rows[phrase.strip("- ").strip()] = rules.strip()
    return rows


@pytest.mark.parametrize("phrase, rules", [
    ("only I / the owner add", 'shared, create "owner", modify "owner"'),
    ("everyone can add, each edits their own", 'shared, create "members", modify "author"'),
    ("everyone can add, only the owner moderates", 'shared, create "members", modify "owner"'),
    ("remember my choice", "per_user"),
    ("publish to viewers without accounts", 'create "owner"'),
])
def test_contract_maps_request_phrases_to_rules(phrase, rules):
    matches = [r for p, r in _phrase_rows().items() if phrase in p]
    assert matches, f"no phrase row for {phrase!r}"
    assert rules in matches[0], (phrase, matches[0])


def test_contract_says_per_user_omits_create_and_modify():
    assert "per_user: omit create/modify" in _contract()


def test_contract_says_storage_is_a_separate_argument():
    contract = _contract()
    assert "`storage` is a separate top-level argument of create_artifact/edit_artifact" in contract
    assert "never inside `prompt`" in contract


_NO_FORM_RULE = "Never use <form onSubmit> for writes"


@pytest.mark.parametrize("index", [0, 1, 2])
def test_example_uses_no_form(index):
    # Artifact iframes are sandboxed without allow-forms: submit never fires in the hosts.
    name, _, code = _examples()[index]
    assert "<form" not in code, name


def test_contract_and_runtime_prompt_forbid_form_submit():
    app_data = SANDBOX_RUNTIME_PROMPT[SANDBOX_RUNTIME_PROMPT.index("APP DATA"):]
    for text in (_contract(), app_data):
        assert _NO_FORM_RULE in text
        assert "button onClick" in text and "Enter" in text

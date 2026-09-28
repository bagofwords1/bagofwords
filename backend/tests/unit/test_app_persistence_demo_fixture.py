"""The app persistence demo app (tools/agent) passes the real storage gates.

The fixture is POSTed to /api/artifacts by tools/agent/seed_app_persistence_demo.py
with the declaration defined there; both must satisfy the same checks the
agent tools apply (declaration strictness, direct string-literal
useCollection calls naming declared collections only).
"""
import importlib.util
from pathlib import Path

import pytest

from app.ai.tools.implementations._artifact_storage import (
    referenced_collections,
    storage_reference_errors,
)
from app.schemas.app_storage import parse_storage_declaration

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tools" / "agent" / "fixtures" / "app_persistence_demo.jsx"
SEED = ROOT / "tools" / "agent" / "seed_app_persistence_demo.py"


def _seed_module():
    spec = importlib.util.spec_from_file_location("seed_app_persistence_demo", SEED)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def seed():
    return _seed_module()


@pytest.fixture(scope="module")
def code():
    return FIXTURE.read_text(encoding="utf-8")


def test_declaration_has_the_three_demo_collections_with_their_rules(seed):
    decl = parse_storage_declaration(seed.STORAGE)
    assert decl is not None
    cols = decl.collections
    assert sorted(cols) == ["highlights", "notes", "selections"]
    assert (cols["notes"].scope, cols["notes"].create, cols["notes"].modify) == ("shared", "members", "author")
    assert cols["selections"].scope == "per_user"
    assert cols["selections"].fields["genre"].type == "string"
    assert cols["selections"].fields["genre"].has_default and cols["selections"].fields["genre"].default is None
    assert (cols["highlights"].scope, cols["highlights"].create, cols["highlights"].modify) == ("shared", "owner", "owner")
    assert cols["notes"].fields["country"].required and cols["notes"].fields["text"].required
    # Only the owner's highlights are published through the public link.
    assert cols["highlights"].public_read is True
    assert cols["notes"].public_read is False and cols["selections"].public_read is False


def test_existing_demo_with_another_declaration_needs_force_new(seed):
    import copy

    older = copy.deepcopy(seed.STORAGE)
    del older["collections"]["highlights"]["public_read"]
    assert seed.demo_action(None, force_new=False) == "create"
    assert seed.demo_action({"content": {"storage": copy.deepcopy(seed.STORAGE)}}, force_new=False) == "reuse"
    # Never mutate an installed demo silently: a different declaration stops the run.
    assert seed.demo_action({"content": {"storage": older}}, force_new=False) == "declaration_differs"
    assert seed.demo_action({"content": {}}, force_new=False) == "declaration_differs"
    assert seed.demo_action({"content": {"storage": older}}, force_new=True) == "create"


def test_fixture_passes_the_use_collection_gate(seed, code):
    decl = parse_storage_declaration(seed.STORAGE)
    assert storage_reference_errors(code, decl) == []
    assert sorted(set(referenced_collections(code))) == ["highlights", "notes", "selections"]


def test_fixture_is_rejected_without_the_declaration(code):
    # The gate is real: the same code without storage names every collection.
    errors = " ".join(storage_reference_errors(code, None))
    for name in ("notes", "selections", "highlights"):
        assert name in errors


def test_fixture_shape_matches_the_page_runtime(code):
    assert code.lstrip().startswith('<script type="text/babel">')
    assert "import " not in code.split("\n", 1)[1].split("ReactDOM")[0].replace("important", "")
    # Viz ids are substituted by the seed script, never hardcoded.
    assert "__REVENUE_VIZ_ID__" in code and "__CUSTOMERS_VIZ_ID__" in code


def test_render_code_substitutes_viz_ids(seed, code):
    out = seed.render_code(code, revenue_id="rev-1", customers_id="cus-2")
    assert "__REVENUE_VIZ_ID__" not in out and "__CUSTOMERS_VIZ_ID__" not in out
    assert "'rev-1'" in out and "'cus-2'" in out


def test_payload_carries_only_the_visualizations_the_code_binds(seed, code):
    # The source dashboard can have more charts than the demo renders; an unused
    # one in the payload makes every later edit_artifact fail the viz-ref gate.
    from app.ai.tools.implementations._artifact_refs import viz_reference_errors

    ids = seed.payload_visualization_ids("rev-1", "cus-2")
    assert ids == ["rev-1", "cus-2"]
    rendered = seed.render_code(code, "rev-1", "cus-2")
    payload = {"visualizations": [{"id": i, "title": i} for i in ids]}
    assert viz_reference_errors(rendered, payload) == []
    # What the script used to send: every source chart, one of them unused.
    payload["visualizations"].append({"id": "gen-3", "title": "Genres"})
    assert viz_reference_errors(rendered, payload)


def test_pick_visualizations_by_title():
    mod = _seed_module()
    vizzes = [
        {"id": "a", "title": "Customers per Country"},
        {"id": "b", "title": "Revenue by Country"},
        {"id": "c", "title": "Tracks"},
    ]
    assert mod.pick_visualizations(vizzes) == ("b", "a")
    # Falls back to order when titles say nothing.
    assert mod.pick_visualizations([{"id": "x", "title": "One"}, {"id": "y", "title": "Two"}]) == ("x", "y")
    assert mod.pick_visualizations([]) == ("", "")


@pytest.mark.parametrize(
    "title,suffix,ok",
    [
        ("Country Revenue by Genre (demo copy)", " (demo copy)", True),
        ("Country Revenue by Genre", " (demo copy)", False),
        ("(demo copy) Country Revenue", " (demo copy)", False),
        (" (demo copy)", " (demo copy)", False),
        ("", " (demo copy)", False),
        (None, " (demo copy)", False),
        ("Any report", None, True),
        ("Any report", "", True),
    ],
)
def test_title_suffix_check_is_opt_in_and_strict(title, suffix, ok):
    assert _seed_module().title_has_suffix(title, suffix) is ok


def test_fixture_does_not_rely_on_form_submission(code):
    # Artifact iframes are sandboxed without allow-forms (ArtifactFrame.vue,
    # pages/r/[id]/index.vue): a submit event never fires there, so every
    # write must hang off a click or key handler.
    assert "<form" not in code
    assert 'type="submit"' not in code
    assert code.count('data-testid="note-add"') == 1 and "onClick={add}" in code

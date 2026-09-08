"""The diagnosis query grammar, held to the golden fixture shared with the
TypeScript parser (frontend/utils/diagnosisQuery.ts).

Every case in ``tests/fixtures/diagnosis_queries.json`` is either
``{q, ast, canonical, tokens}`` or ``{q, error: {position, message,
suggestions}}``. Both parsers must agree on all of it, byte for byte.
"""
import json
from pathlib import Path

import pytest

from app.services.diagnosis import fields as F
from app.services.diagnosis.grammar import (
    MAX_DEPTH,
    MAX_LENGTH,
    MAX_TERMS,
    QueryError,
    canonical,
    mentions_field,
    parse,
    strip_positions,
    top_level_terms,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "diagnosis_queries.json"
CASES = json.loads(FIXTURE.read_text())["cases"]


def _ids():
    return [c["q"][:40] or "<empty>" for c in CASES]


@pytest.mark.parametrize("case", CASES, ids=_ids())
def test_golden(case):
    if "error" in case:
        with pytest.raises(QueryError) as exc:
            parse(case["q"])
        assert exc.value.to_dict() == case["error"]
        return
    result = parse(case["q"])
    assert strip_positions(result.ast) == case["ast"]
    assert canonical(result.ast) == case["canonical"]
    assert [t.to_dict() for t in result.tokens] == case["tokens"]


@pytest.mark.parametrize("case", [c for c in CASES if "ast" in c], ids=[c["q"][:40] or "<empty>" for c in CASES if "ast" in c])
def test_canonical_round_trips(case):
    """parse(canonical(parse(q))) == parse(q): the canonical form is a valid
    query that means the same thing."""
    first = parse(case["q"])
    again = parse(canonical(first.ast))
    assert _drop_raw(strip_positions(again.ast)) == _drop_raw(strip_positions(first.ast))
    assert canonical(again.ast) == canonical(first.ast)


def _drop_raw(node):
    """The canonical form normalises spelling (Status:ERROR → status:error), so
    the literal ``raw`` text legitimately differs; everything else must not."""
    if isinstance(node, dict):
        return {k: _drop_raw(v) for k, v in node.items() if k != "raw"}
    if isinstance(node, list):
        return [_drop_raw(x) for x in node]
    return node


def test_tokens_cover_every_non_space_character():
    """Highlighting relies on tokens tiling the query: every non-space
    character belongs to exactly one token."""
    for case in CASES:
        if "ast" not in case:
            continue
        q = case["q"]
        covered = [False] * len(q)
        for t in case["tokens"]:
            for i in range(t["start"], t["end"]):
                assert not covered[i], f"overlap at {i} in {q!r}"
                covered[i] = True
        for i, ch in enumerate(q):
            assert covered[i] or ch.isspace(), f"{q!r}: char {i} ({ch!r}) uncovered"


def test_every_error_has_a_position_inside_the_query():
    for case in CASES:
        if "error" not in case:
            continue
        pos = case["error"]["position"]
        assert 0 <= pos <= len(case["q"]), case["q"]


def test_limits_are_enforced_at_the_boundary():
    assert parse(" ".join(["a"] * MAX_TERMS)).ast["root"] is not None
    with pytest.raises(QueryError):
        parse(" ".join(["a"] * (MAX_TERMS + 1)))
    assert parse("x" * MAX_LENGTH).ast["root"] is not None
    with pytest.raises(QueryError):
        parse("x" * (MAX_LENGTH + 1))
    assert parse("(" * MAX_DEPTH + "status:error" + ")" * MAX_DEPTH).ast["root"] is not None
    with pytest.raises(QueryError):
        parse("(" * (MAX_DEPTH + 1) + "status:error" + ")" * (MAX_DEPTH + 1))


def test_aliases_resolve_to_the_canonical_field():
    ast = parse("confidence:<3 coverage:>4 tool.name:answer").ast
    fields = [c["field"] for c in ast["root"]["c"]]
    assert fields == ["judge.confidence", "judge.instructions", "tool"]


def test_unknown_field_suggests_close_names():
    with pytest.raises(QueryError) as exc:
        parse("durations:>3s")
    assert "duration" in exc.value.suggestions
    with pytest.raises(QueryError) as exc:
        parse("toool:x")
    assert exc.value.suggestions and exc.value.suggestions[0] in ("tool", "tools")


def test_top_level_terms_drive_quick_filter_state():
    ast = parse("status:error tool:create_data tool.status:error user:dana").ast
    names = [t["field"] for t in top_level_terms(ast)]
    assert names == ["status", "tool", "tool.status", "user"]
    # An OR at the root has no top-level conjunction
    assert top_level_terms(parse("status:error OR user:dana").ast) == []
    # A single term is its own conjunction
    assert [t["field"] for t in top_level_terms(parse("status:error").ast)] == ["status"]


def test_mentions_field_walks_the_whole_tree():
    ast = parse("NOT (status:error OR eval:true)").ast
    assert mentions_field(ast, "eval")
    assert not mentions_field(ast, "user")
    assert not mentions_field(parse("").ast, "eval")


def test_every_registry_field_parses_with_a_typical_value():
    samples = {
        "enum": lambda f: f.values[0],
        "text": lambda f: "x",
        "number": lambda f: "3",
        "duration": lambda f: "3s",
        "money": lambda f: "$1",
        "date": lambda f: "2025-09-01",
        "boolean": lambda f: "true",
        "id": lambda f: "abc",
    }
    for f in F.FIELDS:
        q = f"{f.name}:{samples[f.type](f)}"
        ast = parse(q).ast
        assert ast["root"]["field"] == f.name, q


def test_public_fields_are_json_serialisable_and_complete():
    pub = F.public_fields()
    assert {p["name"] for p in pub} == {f.name for f in F.FIELDS}
    json.dumps(pub)
    for p in pub:
        assert p["type"] in ("enum", "text", "number", "duration", "money", "date", "boolean", "id")
        assert p["entity"] in ("run", "tool")

"""Id-keyed viz reference contract: codemod + gate (app/ai/tools/implementations/_artifact_refs.py)
and the pinned-decisions digest (edit_artifact_legacy.build_pinned_decisions).

The behaviors promised:
- legacy positional `viz[N]` / `data.visualizations[N]` references migrate
  deterministically to `vizById("<uuid>")` against the stored id order;
- the gate flags unknown id refs, out-of-range positional refs, and payload
  vizs the code never renders — and stays silent on conforming code;
- the digest is bounded and keeps both the original intent and recent edits.
"""

import pytest

from app.ai.tools.implementations._artifact_refs import (
    migrate_positional_viz_refs,
    viz_reference_errors,
)
from app.ai.tools.implementations.edit_artifact_legacy import build_pinned_decisions


IDS = [
    "11111111-aaaa-bbbb-cccc-000000000001",
    "22222222-aaaa-bbbb-cccc-000000000002",
    "33333333-aaaa-bbbb-cccc-000000000003",
]


def _payload(ids=IDS, titles=None):
    titles = titles or [f"Viz {i}" for i in range(len(ids))]
    return {"visualizations": [{"id": i, "title": t} for i, t in zip(ids, titles)]}


# ── codemod ──────────────────────────────────────────────────────────────────

def test_codemod_rewrites_every_positional_form_by_stored_order():
    code = (
        "const a = viz[0].rows;\n"
        "const b = data.visualizations[2].rows.map(r => r.x);\n"
        "<KPICard viz={viz[1]} />"
    )
    out, n = migrate_positional_viz_refs(code, IDS)
    assert n == 3
    assert f'vizById("{IDS[0]}").rows' in out
    assert f'vizById("{IDS[2]}").rows.map' in out
    assert f'vizById("{IDS[1]}")' in out
    assert "viz[0]" not in out and "viz[1]" not in out and "visualizations[2]" not in out


def test_codemod_leaves_out_of_range_and_non_positional_untouched():
    code = "const a = viz[7].rows; const all = viz.map(v => v.title);"
    out, n = migrate_positional_viz_refs(code, IDS)
    assert n == 0
    assert "viz[7]" in out  # out of range → left for the gate to flag
    assert "viz.map(v => v.title)" in out  # iteration is not an indexed ref


def test_codemod_noop_without_ids_or_code():
    assert migrate_positional_viz_refs("", IDS) == ("", 0)
    code = "const a = viz[0].rows;"
    assert migrate_positional_viz_refs(code, []) == (code, 0)


def test_codemod_skips_string_literals_and_comments():
    # A quoted "viz[0]" must not become nested-quote vizById("...") — that
    # would break the code; real references on the same/other lines still
    # migrate.
    code = (
        "const label = 'use viz[0] to access data';\n"
        'const doc = "see viz[1] for details";\n'
        "// legacy note: viz[2] was the table\n"
        "const real = viz[0].rows;"
    )
    out, n = migrate_positional_viz_refs(code, IDS)
    assert n == 1
    assert "'use viz[0] to access data'" in out
    assert '"see viz[1] for details"' in out
    assert "// legacy note: viz[2] was the table" in out
    assert f'vizById("{IDS[0]}").rows' in out


def test_codemod_is_idempotent():
    code = "const a = viz[0].rows;"
    once, _ = migrate_positional_viz_refs(code, IDS)
    twice, n2 = migrate_positional_viz_refs(once, IDS)
    assert twice == once
    assert n2 == 0


# ── gate ─────────────────────────────────────────────────────────────────────

def test_gate_passes_conforming_id_keyed_code():
    code = "".join(f'const v{i} = vizById("{vid}");\n' for i, vid in enumerate(IDS))
    assert viz_reference_errors(code, _payload()) == []


def test_gate_flags_unknown_id_reference():
    ghost = "99999999-aaaa-bbbb-cccc-000000000009"
    code = (
        "".join(f'const v{i} = vizById("{vid}");\n' for i, vid in enumerate(IDS))
        + f'const gone = vizById("{ghost}");'
    )
    errors = viz_reference_errors(code, _payload())
    assert any(ghost in e for e in errors)


def test_gate_flags_out_of_range_positional_reference():
    code = "".join(f'const v{i} = vizById("{vid}");\n' for i, vid in enumerate(IDS)) + "const x = viz[9].rows;"
    errors = viz_reference_errors(code, _payload())
    assert any("viz[9]" in e for e in errors)


def test_gate_flags_payload_viz_never_referenced():
    # References only the first two of three payload vizs → the third silently
    # disappears from the dashboard; the gate must say which one.
    code = f'const a = vizById("{IDS[0]}"); const b = vizById("{IDS[1]}");'
    errors = viz_reference_errors(code, _payload(titles=["Rev", "Orders", "Churn"]))
    assert any(IDS[2] in e and "Churn" in e for e in errors)


def test_gate_flags_chartless_page():
    # Reviewer counterexample: a page that references NONE of its payload
    # vizs renders no data and must fail the gate.
    code = "const x = <div className='p-8'>Hello</div>;"
    errors = viz_reference_errors(code, _payload())
    assert any("NONE" in e for e in errors)


def test_gate_slice_is_not_coverage():
    # Reviewer counterexample: viz.slice(0, 1) renders one viz, not all —
    # it must not satisfy coverage.
    code = "const first = viz.slice(0, 1); const n = viz.length;"
    errors = viz_reference_errors(code, _payload())
    assert errors, "slice/length alone must not count as full coverage"


def test_gate_accepts_whole_list_iteration_as_coverage():
    # Code that maps the whole list touches every viz without naming ids.
    code = "const cards = data.visualizations.map(v => card(v));"
    assert viz_reference_errors(code, _payload()) == []


def test_gate_tolerates_legacy_in_range_positional_code():
    # A legacy artifact that references every viz positionally is valid —
    # the gate must not force migration on read.
    code = "const a = viz[0]; const b = viz[1]; const c = viz[2];"
    assert viz_reference_errors(code, _payload()) == []


# ── binding style: coverage must not depend on the accessor's NAME ──────────
# Regression suite for a production incident (report 903c375a): a dashboard
# that bound every payload viz via `.find(v => v.id === "<uuid>")` was rejected
# with "The code references NONE of the visualizations", costing a full
# regeneration. vizById() IS that lookup — see artifact-globals.js.

def test_gate_accepts_find_by_id_as_binding():
    """Attempt 2 of the incident: correct ids, inlined lookup. Must pass."""
    code = "\n".join(
        f'const v{n} = data.visualizations.find(v => v.id === "{vid}");'
        for n, vid in enumerate(IDS)
    )
    assert viz_reference_errors(code, _payload()) == []


def test_gate_accepts_id_keyed_map_and_bracket_lookup():
    """Other spellings that bind by id without naming the helper."""
    code = (
        f'const BY_ID = {{ mom: "{IDS[0]}", ord: "{IDS[1]}" }};\n'
        f'const churn = byId[\'{IDS[2]}\'];'
    )
    assert viz_reference_errors(code, _payload()) == []


def test_gate_accepts_mixed_binding_styles():
    code = (
        f'const a = vizById("{IDS[0]}");\n'
        f'const b = vizs.find(v => v.id === "{IDS[1]}");\n'
        f'const c = lookup["{IDS[2]}"];'
    )
    assert viz_reference_errors(code, _payload()) == []


def test_gate_still_flags_unknown_id_alongside_full_coverage():
    """Attempt 1 of the incident: every payload viz bound correctly, plus one
    invented id. The unknown-ref check is scoped to vizById() and must still
    fire — that was a true positive worth keeping."""
    ghost = "99999999-dead-beef-cccc-000000000009"
    code = "\n".join(f'const v = vizById("{vid}");' for vid in IDS)
    code += f'\nconst missing = vizById("{ghost}");'
    errors = viz_reference_errors(code, _payload())
    assert len(errors) == 1, errors
    assert ghost in errors[0] and "no visualization with that id" in errors[0]


def test_gate_still_flags_missing_viz_under_new_matching():
    """Widening coverage must not mask a genuinely dropped chart."""
    code = f'const a = vizs.find(v => v.id === "{IDS[0]}");'
    errors = viz_reference_errors(code, _payload(titles=["Rev", "Orders", "Churn"]))
    assert any(IDS[2] in e and "Churn" in e for e in errors)


def test_gate_bare_unquoted_uuid_is_not_a_reference():
    """A uuid must be a string literal to count — prose mentioning one does
    not bind anything."""
    code = f"// dashboard for {IDS[0]} {IDS[1]} {IDS[2]}\nconst x = <div>hi</div>;"
    errors = viz_reference_errors(code, _payload())
    assert any("NONE" in e for e in errors)


def test_gate_empty_payload_and_empty_code():
    assert viz_reference_errors("", _payload()) == []
    assert viz_reference_errors("const x = 1;", {"visualizations": []}) == []


# ── pinned decisions digest ──────────────────────────────────────────────────

def test_digest_keeps_original_intent_and_recent_edits():
    spec = "Build a sales dashboard with KPIs on top"
    for v in range(2, 6):
        spec += f"\n+ Edit (v{v}): change {v}"
    digest = build_pinned_decisions(spec)
    assert "Build a sales dashboard" in digest
    assert "change 5" in digest and "change 2" in digest


def test_digest_is_bounded_for_long_histories():
    spec = "x" * 50_000
    for v in range(2, 120):
        spec += f"\n+ Edit (v{v}): " + ("y" * 2_000)
    digest = build_pinned_decisions(spec)
    # Head + 15 edit lines, each clipped — must stay prompt-safe.
    assert len(digest) < 8_000
    # Most recent edits survive; ancient ones are dropped.
    assert "Later edit" in digest
    assert digest.count("- Later edit:") <= 15


def test_digest_empty_spec():
    assert build_pinned_decisions(None) == ""
    assert build_pinned_decisions("") == ""

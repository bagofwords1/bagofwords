"""MCP instruction tools behave like the web app (and edit like the in-app agent).

Contract:
- create / edit / delete over MCP give the same answer and leave the same
  state as the web app's REST routes (what the UI calls) for every persona:
  allowed ⇔ allowed, and the resulting text, status, agent scope and live-ness
  match. Each pair runs on twin instructions so the two paths can't interfere.
- edit_instruction takes the in-app agent's edit contract: text edits are
  anchored on an exact snippet (``old_text``), whole rewrites are an explicit
  ``replace_entire_text``, status is ``active``/``inactive``, and the
  resulting text has a minimum length. Unlike the in-app agent (which stages a
  suggestion), the edit is published — like a save in the web app.
"""

import uuid

import pytest

from app.ai.tools.implementations.edit_instruction import _apply_anchor_edit
from tests.fixtures.user import _pending_invite_token
from tests.mocks import mcp_acl_matrix as M
from tests.mocks.mcp_acl_world import build_world


@pytest.fixture
def world(test_client, enterprise_license):
    return build_world(test_client, _pending_invite_token)


def _report(mismatches, what):
    assert not mismatches, f"{what}: {len(mismatches)} cell(s) differ\n" + "\n".join(
        f"  - {m}" for m in mismatches
    )


def _state(world, inst_id, reader):
    """What a user of the web app would see, or None if it's gone."""
    r = world.rest_get_instruction(reader, inst_id)
    if r.status_code == 404:
        return None
    assert r.status_code == 200, r.text
    b = r.json()
    return {
        "text": b.get("text"),
        "status": b.get("status"),
        "agents": sorted(d["id"] for d in b.get("data_sources") or []),
        "live": b.get("current_build_status") != "pending_approval",
    }


def _twins(world, key):
    scope = M.INSTRUCTION_TARGETS[key]
    author = M.AGENT_OWNER if key == "inst_owned" else M.ORG_ADMIN
    text = f"twin {key} rule {uuid.uuid4().hex[:8]}"
    return (world.create_instruction_as(author, text, scope),
            world.create_instruction_as(author, text, scope),
            world.truth_reader(scope))


def _rest(world, persona, method, path, body=None):
    fn = getattr(world.http, method)
    kwargs = {"headers": world.personas[persona].headers()}
    if body is not None:
        kwargs["json"] = body
    return fn(path, **kwargs)


# ── edit ─────────────────────────────────────────────────────────────────

EDITS = {
    # name → (REST PUT body, MCP edit args); both express the same change.
    "rewrite": (lambda: {"text": "Rewritten rule: fiscal year starts in July."},
                lambda: {"text": "Rewritten rule: fiscal year starts in July.", "replace_entire_text": True}),
    "turn_off": (lambda: {"status": "draft"}, lambda: {"status": "inactive"}),
    "load_mode": (lambda: {"load_mode": "intelligent"}, lambda: {"load_mode": "intelligent"}),
}


def test_edit_matches_the_web_app_for_every_persona(world):
    mismatches = []
    for key in M.INSTRUCTION_TARGETS:
        for persona in M.ALL:
            for name, (rest_body, mcp_args) in EDITS.items():
                a, b, reader = _twins(world, key)
                rest_ok = _rest(world, persona, "put", f"/api/instructions/{a}", rest_body()).status_code == 200
                res = world.mcp(persona).call("edit_instruction", {"instruction_id": b, **mcp_args()})
                label = f"{persona} {key} {name}"
                if rest_ok != res.ok:
                    mismatches.append(f"{label}: web app ok={rest_ok}, MCP ok={res.ok} ({res.summary()})")
                    continue
                sa, sb = _state(world, a, reader), _state(world, b, reader)
                if sa != sb:
                    mismatches.append(f"{label}: web app left {sa}, MCP left {sb}")
    _report(mismatches, "edit parity")


def test_scope_changes_match_the_web_app(world):
    mismatches = []
    for persona, key, scope, _ in M.SCOPE_CHANGES:
        a, b, reader = _twins(world, key)
        ds = [world.agent_id(x) for x in scope]
        rest_ok = _rest(world, persona, "put", f"/api/instructions/{a}", {"data_source_ids": ds}).status_code == 200
        res = world.mcp(persona).call("edit_instruction", {"instruction_id": b, "data_source_ids": ds})
        label = f"{persona} {key} → {scope or 'GLOBAL'}"
        if rest_ok != res.ok:
            mismatches.append(f"{label}: web app ok={rest_ok}, MCP ok={res.ok} ({res.summary()})")
            continue
        # Owner-only scopes may move the row out of the reader's sight; read as admin too.
        for r in {reader, M.ORG_ADMIN}:
            sa, sb = _state(world, a, r), _state(world, b, r)
            if sa != sb:
                mismatches.append(f"{label} (read as {r}): web app {sa}, MCP {sb}")
    _report(mismatches, "scope parity")


# ── create ───────────────────────────────────────────────────────────────

def test_create_matches_the_web_app_for_every_persona(world):
    mismatches = []
    for scope in M.CREATE_ALLOWED:
        ds = [world.agent_id(x) for x in scope]
        reader = world.truth_reader(scope)
        for persona in M.ALL:
            text = f"{persona} new rule {uuid.uuid4().hex[:8]}"
            body = {"text": text, "status": "published", "category": "general",
                    "load_mode": "always", "data_source_ids": ds}
            route = "/api/instructions" if ds else "/api/instructions/global"
            r = _rest(world, persona, "post", route, body)
            res = world.mcp(persona).call("create_instruction", {"text": text, "data_source_ids": ds})
            label = f"{persona} on {'+'.join(scope) or 'GLOBAL'}"
            if (r.status_code == 200) != res.ok:
                mismatches.append(f"{label}: web app {r.status_code}, MCP ok={res.ok} ({res.summary()})")
                continue
            if not res.ok or persona == M.OUTSIDER:   # outsider's rows live in their own org
                continue
            sa = _state(world, r.json()["id"], reader)
            sb = _state(world, res.payload["instruction_id"], reader)
            if sa != sb:
                mismatches.append(f"{label}: web app made {sa}, MCP made {sb}")
            if not (sb or {}).get("live"):
                mismatches.append(f"{label}: MCP-created rule is not live")
    _report(mismatches, "create parity")


# ── delete ───────────────────────────────────────────────────────────────

def test_delete_matches_the_web_app_for_every_persona(world):
    mismatches = []
    for key in M.INSTRUCTION_TARGETS:
        for persona in M.ALL:
            a, b, reader = _twins(world, key)
            rest_ok = _rest(world, persona, "delete", f"/api/instructions/{a}").status_code == 200
            res = world.mcp(persona).call("delete_instruction", {"instruction_id": b})
            label = f"{persona} {key}"
            if rest_ok != res.ok:
                mismatches.append(f"{label}: web app ok={rest_ok}, MCP ok={res.ok} ({res.summary()})")
            sa, sb = _state(world, a, reader), _state(world, b, reader)
            if (sa is None) != (sb is None):
                mismatches.append(f"{label}: web app gone={sa is None}, MCP gone={sb is None}")
    _report(mismatches, "delete parity")


# ── the in-app edit contract ─────────────────────────────────────────────

def test_edit_uses_the_in_app_anchored_contract_and_publishes(world):
    mgr = world.mcp(M.AGENT_MANAGER)
    inst = world.create_instruction_as(
        M.ORG_ADMIN, "Exclude cancelled orders. Revenue is net of tax.\nUse UTC   dates.", ["ALPHA"],
    )
    text = lambda: _state(world, inst, M.ORG_ADMIN)["text"]  # noqa: E731
    base = text()

    def call(**args):
        return mgr.call("edit_instruction", {"instruction_id": inst, **args})

    # Text without an anchor, or with "" as one, is refused and changes nothing.
    for args in ({"text": "Something else entirely here."},
                 {"old_text": "", "text": "Something else entirely here."}):
        r = call(**args)
        assert r.refused and r.payload["rejected_reason"] == "anchor_required", r.payload
        assert r.payload["current_text"] == base
        assert text() == base

    # A missing or ambiguous anchor is refused with the current text echoed.
    r = call(old_text="Exclude refunded orders.", text="x")
    assert r.payload["rejected_reason"] == "anchor_not_found" and r.payload["current_text"] == base
    dup = world.create_instruction_as(M.ORG_ADMIN, "Use UTC. Then also: Use UTC. Always.", ["ALPHA"])
    r = mgr.call("edit_instruction", {"instruction_id": dup, "old_text": "Use UTC.", "text": "Use local time."})
    assert r.payload["rejected_reason"] == "ambiguous_anchor", r.payload

    # An anchored edit replaces just the snippet — same result as the in-app
    # helper, including whitespace-tolerant matching — and is live at once.
    anchor, snippet = "Use UTC dates.", "Use UTC dates, never local time."
    expected, err, _ = _apply_anchor_edit(base, anchor, snippet)
    assert err is None
    r = call(old_text=anchor, text=snippet)
    assert r.ok, r.payload
    assert r.payload["previous_text"] == base and r.payload["new_text"] == expected
    assert text() == expected
    st = _state(world, inst, M.ORG_ADMIN)
    assert st["live"] and st["status"] == "published"

    # The result must stay a usable instruction.
    r = call(text="too short", replace_entire_text=True)
    assert r.payload["rejected_reason"] == "result_too_short" and text() == expected

    # Status is active/inactive, as in-app; the web app sees draft/published.
    r = call(status="inactive")
    assert r.ok and r.payload["new_status"] == "inactive", r.payload
    assert _state(world, inst, M.ORG_ADMIN)["status"] == "draft"
    read = world.mcp(M.AGENT_VIEWER).call("read_instruction", {"instruction_id": inst})
    assert read.ok and read.payload["instruction"]["state"] == "inactive"
    assert call(status="inactive").payload["rejected_reason"] == "no_change"
    assert call(status="active").ok and _state(world, inst, M.ORG_ADMIN)["status"] == "published"

    # 'disabled' is not an off switch (in-app rule).
    r = call(load_mode="disabled")
    assert r.payload["rejected_reason"] == "invalid_load_mode"

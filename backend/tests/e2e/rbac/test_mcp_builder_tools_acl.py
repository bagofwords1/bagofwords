"""MCP builder tools (instructions + schema) apply the web app's access rules.

The cast, targets and expected outcomes live in ``tests/mocks/mcp_acl_matrix.py``
(written before the tools); the world is built through the real API by
``tests/mocks/mcp_acl_world.py``; every call goes over the real ``/api/mcp``
JSON-RPC endpoint via ``tests/mocks/bow_mcp_client.py``.

Each test walks one tool across the whole persona × target grid and reports
every mismatching cell at once, so a failure shows the shape of the gap
(e.g. "every agent-level manager refused") rather than its first cell.

Outcomes are checked at two layers: what the tool answered, and what actually
changed — read back through the REST API as the org admin. A refusal that
still mutates, or a success that didn't, both fail.
"""

import uuid

import pytest

from tests.fixtures.user import _pending_invite_token
from tests.mocks import mcp_acl_matrix as M
from tests.mocks.mcp_acl_world import build_world


@pytest.fixture
def world(test_client, enterprise_license):
    return build_world(test_client, _pending_invite_token)


def _report(mismatches, what):
    assert not mismatches, f"{what}: {len(mismatches)} cell(s) wrong\n" + "\n".join(
        f"  - {m}" for m in mismatches
    )


def _text_of(world, inst_id, reader=M.ORG_ADMIN):
    r = world.rest_get_instruction(reader, inst_id)
    return r.json().get("text") if r.status_code == 200 else None


# ── catalog ──────────────────────────────────────────────────────────────

def test_tool_catalog_lists_builder_tools_only_for_people_who_can_use_them(world):
    mismatches = []
    for persona in M.IN_ORG:
        names = world.mcp(persona).tool_names()
        for tool, visible_to in M.TOOL_VISIBLE_TO.items():
            want = persona in visible_to
            if (tool in names) != want:
                mismatches.append(f"{persona}: {tool} {'missing' if want else 'listed'}")
    _report(mismatches, "tools/list")


def test_calling_an_unlisted_tool_is_refused(world):
    """Hiding a tool is not access control: a client can call any name.
    A plain member is listed no builder writes; every one must refuse."""
    member = world.mcp(M.PLAIN_MEMBER)
    listed = member.tool_names()
    pub = world.agent_id("PUB")
    calls = {
        "edit_instruction": {"instruction_id": world.instructions["inst_pub"], "text": "member hijack"},
        "delete_instruction": {"instruction_id": world.instructions["inst_pub"]},
        "create_instruction": {"text": "member rule", "data_source_ids": [pub]},
        "edit_schema": {"data_source_id": pub, "deactivate": ["albums"]},
    }
    mismatches = []
    before_text = _text_of(world, world.instructions["inst_pub"])
    before_tables = world.active_tables("PUB")
    for tool, args in calls.items():
        assert tool not in listed, f"{tool} unexpectedly listed for a plain member"
        res = member.call(tool, args)
        if not res.refused:
            mismatches.append(f"{tool} accepted: {res.summary()}")
    assert _text_of(world, world.instructions["inst_pub"]) == before_text
    assert world.active_tables("PUB") == before_tables
    _report(mismatches, "unlisted tool calls")


# ── instructions: read ───────────────────────────────────────────────────

def test_read_instruction_matches_rest_visibility(world):
    mismatches = []
    for persona in M.ALL:
        client = world.mcp(persona)
        for key, inst_id in world.instructions.items():
            res = client.call("read_instruction", {"instruction_id": inst_id})
            if res.ok:
                detail = (res.payload or {}).get("instruction") or {}
                if detail.get("id") != inst_id or not detail.get("text"):
                    mismatches.append(f"{persona} {key}: ok but payload incomplete {res.payload}")
            if persona in M.INSTRUCTION_READ_MUST_ALLOW.get(key, set()) and res.refused:
                mismatches.append(f"{persona} {key}: refused, must allow ({res.summary()})")
            if persona in M.INSTRUCTION_READ_MUST_DENY.get(key, set()) and res.ok:
                mismatches.append(f"{persona} {key}: allowed, must deny")
            # Parity with the web app for every in-org cell.
            if persona != M.OUTSIDER:
                rest_ok = world.rest_get_instruction(persona, inst_id).status_code == 200
                if rest_ok != res.ok:
                    mismatches.append(f"{persona} {key}: mcp ok={res.ok} but REST ok={rest_ok}")
    _report(mismatches, "read_instruction")


# ── instructions: edit ───────────────────────────────────────────────────

def test_edit_instruction_follows_instruction_authority(world):
    mismatches = []
    for key, inst_id in world.instructions.items():
        allowed = M.INSTRUCTION_WRITE_ALLOWED[key]
        reader = world.truth_reader(M.INSTRUCTION_TARGETS[key])
        for persona in M.ALL:
            before = _text_of(world, inst_id, reader)
            assert before, f"ground truth unreadable for {key}"
            new_text = f"{persona} edits {key} {uuid.uuid4().hex[:6]}"
            res = world.mcp(persona).call("edit_instruction", {"instruction_id": inst_id, "text": new_text})
            after = _text_of(world, inst_id, reader)
            if persona in allowed:
                if res.refused:
                    mismatches.append(f"{persona} {key}: refused, must allow ({res.summary()})")
                elif after != new_text:
                    mismatches.append(f"{persona} {key}: reported success but text is {after!r}")
            else:
                if res.ok:
                    mismatches.append(f"{persona} {key}: allowed, must refuse")
                if after != before:
                    mismatches.append(f"{persona} {key}: refused path still changed the text")
    _report(mismatches, "edit_instruction")


def test_edit_instruction_cannot_widen_scope_past_authority(world):
    mismatches = []
    for persona, key, scope, allowed in M.SCOPE_CHANGES:
        inst_id = world.instructions[key]
        reader = world.truth_reader(M.INSTRUCTION_TARGETS[key])
        before = world.rest_get_instruction(reader, inst_id).json()
        before_ds = sorted(d["id"] for d in before.get("data_sources") or [])
        want_ds = sorted(world.agent_id(a) for a in scope)
        res = world.mcp(persona).call("edit_instruction", {
            "instruction_id": inst_id, "data_source_ids": want_ds,
        })
        after = world.rest_get_instruction(reader, inst_id).json()
        after_ds = sorted(d["id"] for d in after.get("data_sources") or [])
        label = f"{persona} {key} → {scope or 'GLOBAL'}"
        if allowed:
            if res.refused:
                mismatches.append(f"{label}: refused, must allow ({res.summary()})")
            elif after_ds != want_ds:
                mismatches.append(f"{label}: ok but scope is {after_ds}")
        else:
            if res.ok:
                mismatches.append(f"{label}: allowed, must refuse")
            if after_ds != before_ds:
                mismatches.append(f"{label}: refused path still changed scope to {after_ds}")
    _report(mismatches, "edit_instruction scope")


# ── instructions: delete ─────────────────────────────────────────────────

def test_delete_instruction_follows_instruction_authority(world):
    mismatches = []
    for key, scope in M.INSTRUCTION_TARGETS.items():
        allowed = M.INSTRUCTION_WRITE_ALLOWED[key]
        author = M.AGENT_OWNER if key == "inst_owned" else M.ORG_ADMIN
        reader = world.truth_reader(scope)
        for persona in M.ALL:
            inst_id = world.create_instruction_as(author, f"del {key} by {persona}", scope)
            assert world.rest_get_instruction(reader, inst_id).status_code == 200
            res = world.mcp(persona).call("delete_instruction", {"instruction_id": inst_id})
            gone = world.rest_get_instruction(reader, inst_id).status_code == 404
            if persona in allowed:
                if res.refused:
                    mismatches.append(f"{persona} {key}: refused, must allow ({res.summary()})")
                elif not gone:
                    mismatches.append(f"{persona} {key}: reported success but still present")
            else:
                if res.ok:
                    mismatches.append(f"{persona} {key}: allowed, must refuse")
                if gone:
                    mismatches.append(f"{persona} {key}: refused path still deleted it")
    _report(mismatches, "delete_instruction")


# ── instructions: create ─────────────────────────────────────────────────

def test_create_instruction_scope_and_publish_authority(world):
    mismatches = []
    for scope, (may_create, goes_live) in M.CREATE_ALLOWED.items():
        ds_ids = [world.agent_id(a) for a in scope]
        label_scope = "+".join(scope) or "GLOBAL"
        reader = world.truth_reader(scope)
        for persona in M.ALL:
            text = f"{persona} creates on {label_scope} {uuid.uuid4().hex[:6]}"
            res = world.mcp(persona).call("create_instruction", {"text": text, "data_source_ids": ds_ids})
            label = f"{persona} on {label_scope}"
            if persona == M.OUTSIDER and not scope:
                # A global rule in the outsider's OWN org is theirs to write;
                # it must simply never surface in this org.
                if res.ok and world.rest_get_instruction(reader, res.payload["instruction_id"]).status_code != 404:
                    mismatches.append(f"{label}: outsider's rule is visible in this org")
                continue
            if persona not in may_create:
                if res.ok:
                    mismatches.append(f"{label}: allowed, must refuse")
                continue
            if res.refused:
                mismatches.append(f"{label}: refused, must allow ({res.summary()})")
                continue
            inst_id = res.payload.get("instruction_id")
            row = world.rest_get_instruction(reader, inst_id)
            if row.status_code != 200:
                mismatches.append(f"{label}: reported id {inst_id} not readable ({row.status_code})")
                continue
            body = row.json()
            live = body.get("current_build_status") != "pending_approval"
            if live != (persona in goes_live):
                mismatches.append(f"{label}: live={live}, expected live={persona in goes_live}")
            reported_live = res.payload.get("build_status") == "approved"
            if reported_live != live:
                mismatches.append(f"{label}: tool reported build_status={res.payload.get('build_status')} but live={live}")
            got_ds = sorted(d["id"] for d in body.get("data_sources") or [])
            if got_ds != sorted(ds_ids):
                mismatches.append(f"{label}: scope stored as {got_ds}")
    _report(mismatches, "create_instruction")


# ── schema: read ─────────────────────────────────────────────────────────

def test_describe_tables_follows_view_schema(world):
    # A manager deselects one ALPHA table: only managers may still see it.
    hidden = world.agents["ALPHA"]["seed_tables"][0]
    r = world.http.put(
        f"/api/data_sources/{world.agent_id('ALPHA')}/update_tables_status",
        json={"activate": [], "deactivate": [hidden]}, headers=world.admin().headers(),
    )
    assert r.status_code == 200, r.text
    assert hidden not in world.active_tables("ALPHA")

    mismatches = []
    for agent, allowed in M.SCHEMA_READ_ALLOWED.items():
        for persona in M.ALL:
            res = world.mcp(persona).call("describe_tables", {"data_source_id": world.agent_id(agent)})
            label = f"{persona} {agent}"
            if persona not in allowed:
                if res.ok:
                    mismatches.append(f"{label}: allowed, must refuse")
                continue
            if res.refused:
                mismatches.append(f"{label}: refused, must allow ({res.summary()})")
                continue
            tables = {t["name"]: t for t in res.payload.get("tables", [])}
            if not tables:
                mismatches.append(f"{label}: no tables returned")
                continue
            if agent == "ALPHA":
                sees = hidden in tables
                if sees != (persona in M.SEES_INACTIVE_TABLES["ALPHA"]):
                    mismatches.append(f"{label}: sees deselected table={sees}")
            if persona != M.OUTSIDER:
                rest_ok = world.rest_schema(persona, agent).status_code == 200
                if not rest_ok:
                    mismatches.append(f"{label}: mcp allowed but REST full_schema refused")
    _report(mismatches, "describe_tables")


# ── schema: write ────────────────────────────────────────────────────────

def test_edit_schema_follows_manage(world):
    mismatches = []
    for agent, allowed in M.SCHEMA_WRITE_ALLOWED.items():
        for persona in M.ALL:
            before = world.active_tables(agent)
            target = sorted(before)[0]
            res = world.mcp(persona).call("edit_schema", {
                "data_source_id": world.agent_id(agent), "deactivate": [target],
            })
            after = world.active_tables(agent)
            label = f"{persona} {agent}"
            if persona in allowed:
                if res.refused:
                    mismatches.append(f"{label}: refused, must allow ({res.summary()})")
                elif target in after:
                    mismatches.append(f"{label}: reported success but {target} still active")
                else:
                    # restore for the next persona
                    back = world.mcp(M.ORG_ADMIN).call("edit_schema", {
                        "data_source_id": world.agent_id(agent), "activate": [target],
                    })
                    assert back.ok and target in world.active_tables(agent), back.summary()
            else:
                if res.ok:
                    mismatches.append(f"{label}: allowed, must refuse")
                if after != before:
                    mismatches.append(f"{label}: refused path still changed active tables")
    _report(mismatches, "edit_schema")

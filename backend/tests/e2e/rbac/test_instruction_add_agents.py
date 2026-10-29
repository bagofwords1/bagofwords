"""E2E: `edit_instruction` add_agents shares an existing instruction with more
agents of the conversation — as an explicit, reviewable proposal.

The contract:

- the instruction gains exactly the named agents (only agents attached to the
  conversation can be named), and the same-named tables in each of them;
- no agent is ever removed, and an agent outside the conversation is never
  reached even when it has a table of the same name (table_names would reach it);
- a global instruction, an agent with none of the instruction's tables, an
  unknown or ambiguous agent, a mix with table_names, or knowledge mode is
  refused and stages nothing;
- the share is an ordinary version field: it shows as a `scope` field change,
  nothing moves until a reviewer with authority over the new agents accepts it
  (a text-only accept leaves it pending) OR the whole build is published by
  someone with that same authority, which applies it like every other staged
  change — publishing the staged scope is gated on the new agents exactly as an
  accept is;
- a proposal follows the 3-way rule: an edit that does not change scope relative
  to its base proposes nothing, even against a stale version snapshot.
"""
import sqlite3
import tempfile
import uuid
from types import SimpleNamespace

import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _sqlite(*tables):
    f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    conn = sqlite3.connect(f.name)
    for t in tables:
        conn.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY, amount REAL)")
    conn.commit()
    conn.close()
    return f.name


@pytest.fixture
def world(test_client, bootstrap_admin, sqlite_data_source, refresh_schema, create_report,
          update_tables_status_delta, invite_user_to_org, grant_resource):
    admin = bootstrap_admin("share")
    org_id = admin["org_id"]
    # a, b, c share the table names; d is built on a different database; e has
    # Orders but does not use it (inactive). The conversation has every agent
    # but c.
    layout = {"a": ("Orders", "Invoices"), "b": ("Orders", "Invoices"),
              "c": ("Orders",), "d": ("Customers",), "e": ("Orders",), "f": ("Orders",),
              "g": ("Ledger",)}
    ds = {}
    for key, tables in layout.items():
        d = sqlite_data_source(name=f"{key}_{uuid.uuid4().hex[:6]}", user_token=admin["token"],
                               org_id=org_id, database=_sqlite(*tables))
        rows = refresh_schema(data_source_id=d["id"], user_token=admin["token"], org_id=org_id)
        d["tables"] = {t["name"]: t["id"] for t in rows}
        update_tables_status_delta(data_source_id=d["id"],
                                   activate=[] if key == "e" else list(tables),
                                   deactivate=list(tables) if key == "e" else [],
                                   user_token=admin["token"], org_id=org_id)
        ds[key] = d
    report = create_report(title="share", user_token=admin["token"], org_id=org_id,
                           data_sources=[ds[k]["id"] for k in ("a", "b", "d", "e", "f", "g")])
    def _manager_of(*keys):
        u = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
        for k in keys:
            r = grant_resource(resource_type="data_source", resource_id=ds[k]["id"],
                               principal_type="user", principal_id=u["user_id"],
                               permissions=["manage_instructions"],
                               user_token=admin["token"], org_id=org_id)
            assert r.status_code == 200, r.text
        return u

    return {"org_id": org_id, "admin": admin, "report_id": report["id"],
            "report_agents": [ds[k]["id"] for k in ("a", "b", "d", "e", "f", "g")],
            "manager_of": _manager_of, **ds}


def _instruction(test_client, world, agent_keys, tables=("Orders",)):
    body = {"text": "Amounts are net of tax.", "status": "published", "category": "general",
            "data_source_ids": [world[k]["id"] for k in agent_keys],
            "references": [{"object_type": "datasource_table", "object_id": world[k]["tables"][t],
                            "display_text": t} for k in agent_keys for t in tables]}
    r = test_client.post("/api/instructions", json=body,
                         headers=_hdr(world["admin"]["token"], world["org_id"]))
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _new_build(world):
    from app.dependencies import async_session_maker
    from app.services.build_service import BuildService
    from tests.utils.real_execution import make_agent_execution
    exec_id = await make_agent_execution(world["org_id"], world["admin"]["user_id"])
    async with async_session_maker() as db:
        build = await BuildService().get_or_create_draft_build(
            db, org_id=str(world["org_id"]), source="ai",
            user_id=str(world["admin"]["user_id"]), agent_execution_id=exec_id)
        return str(build.id)


async def _edit(world, tool_input, build_id=None, mode="training"):
    """Run edit_instruction in a session of the world's conversation."""
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool
    from app.dependencies import async_session_maker
    build_id = build_id or await _new_build(world)
    async with async_session_maker() as db:
        ctx = {"db": db, "user": SimpleNamespace(id=world["admin"]["user_id"]),
               "organization": SimpleNamespace(id=world["org_id"]), "mode": mode,
               "report": SimpleNamespace(id=world["report_id"], data_sources=[
                   SimpleNamespace(id=d) for d in world.get("report_agents", [])]),
               "training_build_id": build_id}
        end = None
        async for evt in EditInstructionTool().run_stream({**tool_input, "evidence": "test"}, ctx):
            if evt.type == "tool.error":
                pytest.fail(f"tool errored: {evt.payload}")
            if evt.type == "tool.end":
                end = evt
    assert end is not None
    return build_id, end.payload["output"]


def _live(test_client, world, iid):
    r = test_client.get(f"/api/instructions/{iid}", headers=_hdr(world["admin"]["token"], world["org_id"]))
    assert r.status_code == 200, r.text
    j = r.json()
    return ({d["id"] for d in j.get("data_sources") or []},
            {x["object_id"] for x in j.get("references") or [] if x["object_type"] == "datasource_table"})


def _review(test_client, world, iid, token=None):
    r = test_client.get(f"/api/instructions/{iid}/review-hunks",
                        headers=_hdr(token or world["admin"]["token"], world["org_id"]))
    assert r.status_code == 200, r.text
    return r.json()


def _scope_changes(review, build_id):
    """Every scope row of one suggestion — one per agent it shares with."""
    s = next((s for s in review["suggestions"] if s["build_id"] == build_id), None)
    return [f for f in (s or {}).get("field_changes", []) if f["field"] == "scope"]


def _scope_change(review, build_id):
    """The share as one decision: the union of its per-agent rows, or None."""
    rows = _scope_changes(review, build_id)
    if not rows:
        return None
    to = sorted({a for r in rows for a in r["to"]})
    return {"from": rows[0]["from"], "to": to, "to_names": sorted({n for r in rows for n in r["to_names"]}),
            "keys": [r["key"] for r in rows], "key": rows[0]["key"], "rows": rows}


def _resolve(test_client, world, iid, review, keys, action, token=None):
    return test_client.post(
        f"/api/instructions/{iid}/hunks/{action}-all",
        headers=_hdr(token or world["admin"]["token"], world["org_id"]),
        json={"against_main_build_id": review["main_build_id"],
              "against_main_version_id": review["main_version_id"],
              "hunks": [{"build_id": bid, "hunk_key": key} for bid, key in keys]},
    )


def _accept(test_client, world, iid, build_id, token=None, expect=200):
    review = _review(test_client, world, iid, token=token)
    change = _scope_change(review, build_id)
    assert change is not None
    r = _resolve(test_client, world, iid, review, [(build_id, k) for k in change["keys"]], "accept", token=token)
    assert r.status_code == expect, r.text
    return change


def _pending(test_client, world, iid):
    r = test_client.get("/api/instructions/pending-changes",
                        headers=_hdr(world["admin"]["token"], world["org_id"]))
    assert r.status_code == 200, r.text
    return iid in r.json()["instruction_ids"]


async def _diverge(iid, agent_ids):
    """Make main's version snapshot name agents the live row is not attached
    to — what older whole-build publishes left behind. The API cannot produce
    this state any more, hence the direct write."""
    from sqlalchemy import update
    from app.dependencies import async_session_maker
    from app.models.instruction import Instruction
    from app.models.instruction_version import InstructionVersion
    async with async_session_maker() as db:
        vid = (await db.get(Instruction, iid)).current_version_id
        await db.execute(update(InstructionVersion).where(InstructionVersion.id == vid)
                         .values(data_source_ids=list(agent_ids)))
        await db.commit()




@pytest.mark.asyncio
async def test_add_agents_is_a_pending_scope_change_with_agent_names(test_client, world):
    iid = _instruction(test_client, world, ["a"])
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"].upper()]})
    assert out["success"], out

    change = _scope_change(_review(test_client, world, iid), bid)
    assert change is not None, "a share must be reviewable"
    assert set(change["from"]) == {world["a"]["id"]}
    assert set(change["to"]) == {world["a"]["id"], world["b"]["id"]}
    assert set(change["to_names"]) == {world["a"]["name"], world["b"]["name"]}
    assert _pending(test_client, world, iid)
    # A suggestion only — nothing moved yet.
    assert _live(test_client, world, iid) == ({world["a"]["id"]}, {world["a"]["tables"]["Orders"]})


@pytest.mark.asyncio
async def test_accepting_shares_with_exactly_the_named_agent_and_its_tables(test_client, world):
    iid = _instruction(test_client, world, ["a"])
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    _accept(test_client, world, iid, bid)

    agents, tables = _live(test_client, world, iid)
    assert agents == {world["a"]["id"], world["b"]["id"]}  # c has Orders too, and stays out
    assert tables == {world["a"]["tables"]["Orders"], world["b"]["tables"]["Orders"]}
    assert not _pending(test_client, world, iid)
    assert _scope_change(_review(test_client, world, iid), bid) is None


@pytest.mark.asyncio
async def test_add_agents_on_an_instruction_without_tables_adds_the_agent(test_client, world):
    iid = _instruction(test_client, world, ["a"], tables=())
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["d"]["name"]]})
    assert out["success"], out
    _accept(test_client, world, iid, bid)
    assert _live(test_client, world, iid) == ({world["a"]["id"], world["d"]["id"]}, set())


@pytest.mark.asyncio
async def test_rejecting_the_share_changes_nothing(test_client, world):
    iid = _instruction(test_client, world, ["a"])
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    review = _review(test_client, world, iid)
    change = _scope_change(review, bid)
    r = _resolve(test_client, world, iid, review, [(bid, k) for k in change["keys"]], "reject")
    assert r.status_code == 200, r.text
    assert _live(test_client, world, iid) == ({world["a"]["id"]}, {world["a"]["tables"]["Orders"]})
    assert not _pending(test_client, world, iid)
    assert _scope_change(_review(test_client, world, iid), bid) is None


@pytest.mark.asyncio
async def test_accepting_only_the_text_keeps_the_agents_and_leaves_the_share_pending(test_client, world):
    iid = _instruction(test_client, world, ["a"])
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    _bid, out = await _edit(world, {"instruction_id": iid, "old_text": "net of tax",
                                    "text": "net of tax and fees"}, build_id=bid)
    assert out["success"], out
    review = _review(test_client, world, iid)
    text_keys = [(bid, h["key"]) for h in next(s for s in review["suggestions"] if s["build_id"] == bid)["hunks"]]
    assert text_keys

    r = _resolve(test_client, world, iid, review, text_keys, "accept")
    assert r.status_code == 200, r.text
    assert _live(test_client, world, iid)[0] == {world["a"]["id"]}
    change = _scope_change(_review(test_client, world, iid), bid)
    assert change is not None and set(change["to"]) == {world["a"]["id"], world["b"]["id"]}


@pytest.mark.asyncio
async def test_publishing_a_build_applies_the_staged_share(test_client, world):
    """Scope is an ordinary staged field, so a whole-build publish applies it
    with the text: the new agent and its tables reach the live attachment
    (promotion syncs the version's scope). Publishing the build is approval of
    everything it stages; the per-hunk review is only for granular verdicts."""
    H = _hdr(world["admin"]["token"], world["org_id"])
    iid = _instruction(test_client, world, ["a"])
    bid, out = await _edit(world, {"instruction_id": iid, "old_text": "net of tax", "text": "net of VAT",
                                   "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    r = test_client.post(f"/api/builds/{bid}/publish", headers=H)
    assert r.status_code == 200, r.text
    assert "net of VAT" in _live_text(test_client, world, iid)
    agents, tables = _live(test_client, world, iid)
    assert agents == {world["a"]["id"], world["b"]["id"]}
    assert tables == {world["a"]["tables"]["Orders"], world["b"]["tables"]["Orders"]}


@pytest.mark.asyncio
async def test_rejecting_the_share_then_publishing_keeps_the_agents(test_client, world):
    """A rejected share is undone on the staged version (its base scope is
    re-staged), so publishing the rest of the build applies the text and leaves
    the agents unchanged."""
    H = _hdr(world["admin"]["token"], world["org_id"])
    iid = _instruction(test_client, world, ["a"])
    bid, out = await _edit(world, {"instruction_id": iid, "old_text": "net of tax", "text": "net of VAT",
                                   "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    review = _review(test_client, world, iid)
    change = _scope_change(review, bid)
    r = _resolve(test_client, world, iid, review, [(bid, k) for k in change["keys"]], "reject")
    assert r.status_code == 200, r.text
    r = test_client.post(f"/api/builds/{bid}/publish", headers=H)
    assert r.status_code == 200, r.text
    assert "net of VAT" in _live_text(test_client, world, iid)
    assert _live(test_client, world, iid)[0] == {world["a"]["id"]}


def _live_text(test_client, world, iid):
    r = test_client.get(f"/api/instructions/{iid}", headers=_hdr(world["admin"]["token"], world["org_id"]))
    assert r.status_code == 200, r.text
    return r.json()["text"]


@pytest.mark.asyncio
async def test_accepting_needs_authority_over_every_new_agent(test_client, world):
    iid = _instruction(test_client, world, ["a"])
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    only_a = world["manager_of"]("a")
    _accept(test_client, world, iid, bid, token=only_a["token"], expect=403)
    assert _live(test_client, world, iid)[0] == {world["a"]["id"]}

    both = world["manager_of"]("a", "b")
    _accept(test_client, world, iid, bid, token=both["token"])
    assert _live(test_client, world, iid)[0] == {world["a"]["id"], world["b"]["id"]}


@pytest.mark.asyncio
async def test_accepting_a_move_to_global_needs_org_level_authority(test_client, world):
    """Emptying an instruction's agents makes it GLOBAL — it then reaches every
    agent in the org, so it needs org-level manage_instructions. Managing the
    one agent it is LOSING is not enough: the manager of `a` is refused, an org
    admin succeeds. (table_names=[] is the 'make global' gesture.)"""
    iid = _instruction(test_client, world, ["a"])
    bid, out = await _edit(world, {"instruction_id": iid, "table_names": []})
    assert out["success"], out
    only_a = world["manager_of"]("a")
    _accept(test_client, world, iid, bid, token=only_a["token"], expect=403)
    assert _live(test_client, world, iid)[0] == {world["a"]["id"]}  # unchanged

    _accept(test_client, world, iid, bid, token=world["admin"]["token"])
    assert _live(test_client, world, iid)[0] == set()  # now global


@pytest.mark.asyncio
async def test_publishing_a_move_to_global_needs_org_level_authority(test_client, world):
    """The whole-build publish of a move-to-global is gated the same way: a
    per-agent manager cannot publish it past the gate; an org admin can."""
    H_admin = _hdr(world["admin"]["token"], world["org_id"])
    iid = _instruction(test_client, world, ["a"])
    bid, out = await _edit(world, {"instruction_id": iid, "table_names": []})
    assert out["success"], out
    only_a = world["manager_of"]("a")
    r = test_client.post(f"/api/builds/{bid}/publish", headers=_hdr(only_a["token"], world["org_id"]))
    assert r.status_code == 403, r.text
    assert _live(test_client, world, iid)[0] == {world["a"]["id"]}

    r = test_client.post(f"/api/builds/{bid}/publish", headers=H_admin)
    assert r.status_code == 200, r.text
    assert _live(test_client, world, iid)[0] == set()


@pytest.mark.asyncio
async def test_publishing_needs_authority_over_every_new_agent(test_client, world):
    """A whole-build publish applies the staged scope, so it is gated like an
    accept: the publisher must manage every agent the share ADDS, not only the
    ones the instruction already lives on. A manager of `a` alone must not be
    able to publish a share to `b` past the per-agent gate."""
    iid = _instruction(test_client, world, ["a"])
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]})
    assert out["success"], out

    only_a = world["manager_of"]("a")
    r = test_client.post(f"/api/builds/{bid}/publish", headers=_hdr(only_a["token"], world["org_id"]))
    assert r.status_code == 403, r.text
    assert _live(test_client, world, iid)[0] == {world["a"]["id"]}

    both = world["manager_of"]("a", "b")
    r = test_client.post(f"/api/builds/{bid}/publish", headers=_hdr(both["token"], world["org_id"]))
    assert r.status_code == 200, r.text
    assert _live(test_client, world, iid)[0] == {world["a"]["id"], world["b"]["id"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("case,expected", [
    ("outside_conversation", "agent_not_in_conversation"),
    ("no_matching_tables", "no_matching_tables"),
    ("only_inactive_tables", "no_matching_tables"),
    ("global", "global_instruction"),
    ("already_shared", "no_change"),
    ("with_table_names", "conflicting_scope"),
])
async def test_add_agents_refusals_stage_nothing(test_client, world, case, expected):
    if case == "global":
        iid = _instruction(test_client, world, [], tables=())
    else:
        iid = _instruction(test_client, world, ["a"])
    request = {
        "outside_conversation": {"add_agents": [world["c"]["name"]]},
        "no_matching_tables": {"add_agents": [world["d"]["name"]]},
        "only_inactive_tables": {"add_agents": [world["e"]["name"]]},
        "global": {"add_agents": [world["b"]["name"]]},
        "already_shared": {"add_agents": [world["a"]["name"]]},
        "with_table_names": {"add_agents": [world["b"]["name"]], "table_names": ["Orders"]},
    }[case]
    bid, out = await _edit(world, {"instruction_id": iid, **request})
    assert out["success"] is False
    assert out["rejected_reason"] == expected
    assert not any(s["build_id"] == bid for s in _review(test_client, world, iid)["suggestions"])


@pytest.mark.asyncio
async def test_add_agents_is_refused_in_knowledge_mode(test_client, world):
    iid = _instruction(test_client, world, ["a"])
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]},
                           mode="knowledge")
    assert out["success"] is False
    assert out["rejected_reason"] == "share_not_allowed"
    assert not any(s["build_id"] == bid for s in _review(test_client, world, iid)["suggestions"])


@pytest.mark.asyncio
async def test_a_name_two_agents_share_is_refused(test_client, world, sqlite_data_source,
                                                  create_report):
    admin, org_id = world["admin"], world["org_id"]
    base = f"Sales{uuid.uuid4().hex[:4]}"
    twins = [sqlite_data_source(name=n, user_token=admin["token"], org_id=org_id,
                                database=_sqlite("Orders")) for n in (base, base.lower())]
    report = create_report(title="twins", user_token=admin["token"], org_id=org_id,
                           data_sources=[world["a"]["id"], *(t["id"] for t in twins)])
    iid = _instruction(test_client, world, ["a"])
    bid, out = await _edit({**world, "report_id": report["id"]},
                           {"instruction_id": iid, "add_agents": [base.upper()]})
    assert out["success"] is False
    assert out["rejected_reason"] == "ambiguous_agent"


@pytest.mark.asyncio
async def test_empty_add_agents_is_ignored(test_client, world):
    iid = _instruction(test_client, world, ["a"])
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [],
                                   "old_text": "net of tax", "text": "net of VAT"})
    assert out["success"], out
    assert _scope_change(_review(test_client, world, iid), bid) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True], ids=["clean", "stale_snapshot"])
async def test_an_edit_that_does_not_share_proposes_no_scope_change(test_client, world, stale):
    """Whatever main's version snapshot recorded — including agents the row
    was never attached to — a text edit never turns into a share."""
    iid = _instruction(test_client, world, ["a"])
    if stale:
        await _diverge(iid, [world["a"]["id"], world["b"]["id"], world["c"]["id"]])
    bid, out = await _edit(world, {"instruction_id": iid, "old_text": "net of tax", "text": "gross of tax"})
    assert out["success"], out
    review = _review(test_client, world, iid)
    assert next(s for s in review["suggestions"] if s["build_id"] == bid)["hunks"]
    assert _scope_change(review, bid) is None


@pytest.mark.asyncio
async def test_add_agents_proposes_against_the_live_attachment(test_client, world):
    """Main's snapshot claims b and c; the row is attached to a only. Sharing
    with b — alone, or after a text edit in the same session — proposes
    exactly a + b: b is not mistaken for already shared, c is not dragged in."""
    for text_first in (False, True):
        iid = _instruction(test_client, world, ["a"])
        await _diverge(iid, [world[k]["id"] for k in ("a", "b", "c")])
        bid = None
        if text_first:
            bid, out = await _edit(world, {"instruction_id": iid, "old_text": "net of tax",
                                           "text": "net of tax and fees"})
            assert out["success"], out
        bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]},
                               build_id=bid)
        assert out["success"], out
        change = _scope_change(_review(test_client, world, iid), bid)
        assert set(change["to"]) == {world["a"]["id"], world["b"]["id"]}, text_first


@pytest.mark.asyncio
async def test_two_shares_in_one_session_accumulate(test_client, world):
    iid = _instruction(test_client, world, ["a"], tables=())
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    _bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["d"]["name"]]}, build_id=bid)
    assert out["success"], out
    change = _scope_change(_review(test_client, world, iid), bid)
    assert set(change["to"]) == {world[k]["id"] for k in ("a", "b", "d")}
    _accept(test_client, world, iid, bid)
    assert _live(test_client, world, iid)[0] == {world[k]["id"] for k in ("a", "b", "d")}


@pytest.mark.asyncio
async def test_a_later_edit_in_the_same_session_keeps_the_share(test_client, world):
    iid = _instruction(test_client, world, ["a"])
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    _bid, out = await _edit(world, {"instruction_id": iid, "old_text": "net of tax",
                                    "text": "net of tax and fees"}, build_id=bid)
    assert out["success"], out
    change = _scope_change(_review(test_client, world, iid), bid)
    assert change is not None and set(change["to"]) == {world["a"]["id"], world["b"]["id"]}
    _accept(test_client, world, iid, bid)
    assert _live(test_client, world, iid) == (
        {world["a"]["id"], world["b"]["id"]},
        {world["a"]["tables"]["Orders"], world["b"]["tables"]["Orders"]},
    )


@pytest.mark.asyncio
async def test_an_agent_deleted_before_accept_is_skipped(test_client, world):
    iid = _instruction(test_client, world, ["a"], tables=())
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"], world["d"]["name"]]})
    assert out["success"], out
    r = test_client.delete(f"/api/data_sources/{world['b']['id']}",
                           headers=_hdr(world["admin"]["token"], world["org_id"]))
    assert r.status_code in (200, 204), r.text
    _accept(test_client, world, iid, bid)
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.instruction import instruction_data_source_association as idsa
    async with async_session_maker() as db:
        attached = {str(d) for (d,) in (await db.execute(
            select(idsa.c.data_source_id).where(idsa.c.instruction_id == iid))).all()}
    assert attached == {world["a"]["id"], world["d"]["id"]}


@pytest.mark.asyncio
async def test_a_share_main_already_carries_is_not_pending(test_client, world):
    """A person attaches b in the UI while the AI's share with b sits in
    review: nothing is left to propose, and no second badge appears."""
    iid = _instruction(test_client, world, ["a"], tables=())
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    assert _scope_change(_review(test_client, world, iid), bid) is not None
    r = test_client.put(f"/api/instructions/{iid}", headers=_hdr(world["admin"]["token"], world["org_id"]),
                        json={"data_source_ids": [world["a"]["id"], world["b"]["id"]]})
    assert r.status_code == 200, r.text
    assert _scope_change(_review(test_client, world, iid), bid) is None
    assert not _pending(test_client, world, iid)


@pytest.mark.asyncio
async def test_accepting_applies_exactly_the_versions_scope(test_client, world):
    """The accepted scope IS the agent and its same-named table: b with its
    Orders, not b's Invoices (never referenced) nor a's."""
    iid = _instruction(test_client, world, ["a"])  # a's Orders only
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    _accept(test_client, world, iid, bid)
    agents, tables = _live(test_client, world, iid)
    assert agents == {world["a"]["id"], world["b"]["id"]}
    assert tables == {world["a"]["tables"]["Orders"], world["b"]["tables"]["Orders"]}
    assert world["b"]["tables"]["Invoices"] not in tables


@pytest.mark.asyncio
async def test_sharing_again_with_an_agent_already_proposed_is_no_change(test_client, world):
    iid = _instruction(test_client, world, ["a"], tables=())
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    _bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]}, build_id=bid)
    assert out["success"] is False
    assert out["rejected_reason"] == "no_change"


@pytest.mark.asyncio
async def test_two_shares_in_one_session_keep_both_agents_tables(test_client, world):
    iid = _instruction(test_client, world, ["a"])
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    _bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["f"]["name"]]}, build_id=bid)
    assert out["success"], out
    _accept(test_client, world, iid, bid)
    agents, tables = _live(test_client, world, iid)
    assert agents == {world[k]["id"] for k in ("a", "b", "f")}
    assert tables == {world[k]["tables"]["Orders"] for k in ("a", "b", "f")}


@pytest.mark.asyncio
async def test_a_rejected_share_is_not_re_proposed_and_does_not_linger(test_client, world):
    """Rejecting a share undoes it on the staged version. Proposing the exact
    same scope again is refused; a different share is a fresh proposal, and the
    rejected agent is not silently carried back into it."""
    iid = _instruction(test_client, world, ["a"], tables=())
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    review = _review(test_client, world, iid)
    change = _scope_change(review, bid)
    r = _resolve(test_client, world, iid, review, [(bid, k) for k in change["keys"]], "reject")
    assert r.status_code == 200, r.text

    # The identical share is refused, not humoured.
    _bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]}, build_id=bid)
    assert out["success"] is False and out["rejected_reason"] == "share_rejected"
    # A different agent is a fresh proposal, and b does not come back with it.
    _bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["d"]["name"]]}, build_id=bid)
    assert out["success"], out
    change = _scope_change(_review(test_client, world, iid), bid)
    assert set(change["to"]) == {world["a"]["id"], world["d"]["id"]}, "b must not come back"


@pytest.mark.asyncio
async def test_table_names_staged_earlier_in_the_session_survive_a_text_edit(test_client, world):
    """Main's behaviour: a scoping edit carries over to later edits in the
    same build. (Sharing moved to its own field precisely so this snapshot
    could stay what it was.)"""
    iid = _instruction(test_client, world, ["a"])
    bid, out = await _edit(world, {"instruction_id": iid, "table_names": ["Invoices"]})
    assert out["success"], out
    _bid, out = await _edit(world, {"instruction_id": iid, "old_text": "net of tax", "text": "net of VAT"}, build_id=bid)
    assert out["success"], out
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.build_content import BuildContent
    from app.models.instruction_version import InstructionVersion
    async with async_session_maker() as db:
        vid = (await db.execute(select(BuildContent.instruction_version_id).where(
            BuildContent.build_id == bid, BuildContent.instruction_id == iid))).scalar()
        v = await db.get(InstructionVersion, vid)
    assert {str(r["object_id"]) for r in v.references_json} == {world["a"]["tables"]["Invoices"], world["b"]["tables"]["Invoices"]}


@pytest.mark.asyncio
async def test_table_names_after_a_share_sets_the_tables_wholesale(test_client, world):
    """Scope is one field: a table_names edit after a share sets the table
    scope by its org-wide name match (here Invoices, which a and b share), so
    the later tables replace the share's Orders. Accepting attaches the matched
    agents with those tables."""
    iid = _instruction(test_client, world, ["a"])  # a's Orders
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    _bid, out = await _edit(world, {"instruction_id": iid, "table_names": ["Invoices"]}, build_id=bid)
    assert out["success"], out
    _accept(test_client, world, iid, bid)
    agents, tables = _live(test_client, world, iid)
    assert agents == {world["a"]["id"], world["b"]["id"]}
    assert tables == {world["a"]["tables"]["Invoices"], world["b"]["tables"]["Invoices"]}


@pytest.mark.asyncio
async def test_the_accepted_version_carries_the_new_agents_tables(test_client, world):
    """Intelligent loading reads tables from the live version's snapshot, so
    the version an accept publishes must list b's Orders, not only the row."""
    iid = _instruction(test_client, world, ["a"])
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    _accept(test_client, world, iid, bid)
    from app.dependencies import async_session_maker
    from app.models.instruction import Instruction
    from app.models.instruction_version import InstructionVersion
    async with async_session_maker() as db:
        inst = await db.get(Instruction, iid)
        v = await db.get(InstructionVersion, inst.current_version_id)
    assert {str(r["object_id"]) for r in v.references_json} == {world["a"]["tables"]["Orders"], world["b"]["tables"]["Orders"]}
    assert set(v.data_source_ids) == {world["a"]["id"], world["b"]["id"]}


@pytest.mark.asyncio
async def test_a_scoped_manager_can_accept_when_a_deleted_agent_is_in_the_share(test_client, world):
    iid = _instruction(test_client, world, ["a"], tables=())
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"], world["d"]["name"]]})
    assert out["success"], out
    r = test_client.delete(f"/api/data_sources/{world['d']['id']}",
                           headers=_hdr(world["admin"]["token"], world["org_id"]))
    assert r.status_code in (200, 204), r.text
    only_ab = world["manager_of"]("a", "b")
    _accept(test_client, world, iid, bid, token=only_ab["token"])
    assert _live(test_client, world, iid)[0] == {world["a"]["id"], world["b"]["id"]}


@pytest.mark.asyncio
async def test_approving_and_promoting_a_build_applies_the_share(test_client, world):
    """Every auto-promote (Self Learning included) approves before promoting;
    the staged share is applied along the way, not dropped — promotion syncs
    the version's scope onto the live attachment."""
    iid = _instruction(test_client, world, ["a"], tables=())
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    from app.dependencies import async_session_maker
    from app.services.build_service import BuildService
    async with async_session_maker() as db:
        await BuildService().submit_build(db, bid)
        await BuildService().approve_build(db, bid, approved_by_user_id=world["admin"]["user_id"])
        # The real auto-promote paths (publish, accept, Self Learning) pass
        # apply_scope_changes=True so a staged share reaches the live attachment;
        # system promotes (rollback, git sync, demo) leave scope untouched.
        await BuildService().promote_build(db, bid, user_id=world["admin"]["user_id"], apply_scope_changes=True)
    assert _live(test_client, world, iid)[0] == {world["a"]["id"], world["b"]["id"]}
    assert not _pending(test_client, world, iid)


# --- round-4 review findings --------------------------------------------------

@pytest.mark.asyncio
async def test_a_build_whose_share_was_rejected_publishes_after_main_moved(test_client, world):
    """Finding: the merge build a stale publish creates did not carry the
    reviewer's rejections, so promote refused it AFTER the source build was
    already marked approved — stuck forever as 'already published'."""
    H = _hdr(world["admin"]["token"], world["org_id"])
    iid = _instruction(test_client, world, ["a"], tables=())
    bid, out = await _edit(world, {"instruction_id": iid, "old_text": "net of tax", "text": "net of VAT",
                                   "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    review = _review(test_client, world, iid)
    change = _scope_change(review, bid)
    r = _resolve(test_client, world, iid, review, [(bid, k) for k in change["keys"]], "reject")
    assert r.status_code == 200, r.text
    # main moves: another instruction's build is published
    other = _instruction(test_client, world, ["a"], tables=())
    obid, out = await _edit(world, {"instruction_id": other, "old_text": "net of tax", "text": "net of fees"})
    assert out["success"], out
    assert test_client.post(f"/api/builds/{obid}/publish", headers=H).status_code == 200

    r = test_client.post(f"/api/builds/{bid}/publish", headers=H)
    assert r.status_code == 200, r.text
    assert "net of VAT" in _live_text(test_client, world, iid)
    assert _live(test_client, world, iid)[0] == {world["a"]["id"]}


@pytest.mark.asyncio
async def test_sharing_an_instruction_that_is_not_live_yet_is_refused(test_client, world):
    """A share is reviewed against the live agents; an instruction nobody
    accepted yet has none, so the share could never be shown or published."""
    from types import SimpleNamespace
    from app.ai.tools.implementations.create_instruction import CreateInstructionTool
    from app.dependencies import async_session_maker
    nb = await _new_build(world)
    new_id = None
    async with async_session_maker() as db:
        ctx = {"db": db, "user": SimpleNamespace(id=world["admin"]["user_id"]),
               "organization": SimpleNamespace(id=world["org_id"]), "mode": "training",
               "report": SimpleNamespace(id=world["report_id"], data_sources=[
                   SimpleNamespace(id=d) for d in world["report_agents"]]),
               "training_build_id": nb}
        async for evt in CreateInstructionTool().run_stream(
                {"text": "Ledger postings are always in the booking currency.", "title": "Ledger rule",
                 "category": "general", "confidence": 0.9, "load_mode": "always",
                 "table_names": ["Ledger"], "evidence": "test"}, ctx):
            if evt.type == "tool.end":
                assert evt.payload["output"]["success"], evt.payload
                new_id = evt.payload["output"]["instruction_id"]
    assert new_id
    _b, out = await _edit(world, {"instruction_id": new_id, "add_agents": [world["b"]["name"]]}, build_id=nb)
    assert out["success"] is False
    assert out["rejected_reason"] == "not_live"
    # The create itself still publishes.
    r = test_client.post(f"/api/builds/{nb}/publish", headers=_hdr(world["admin"]["token"], world["org_id"]))
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_a_share_on_an_instruction_made_global_meanwhile_is_dropped_not_applied(test_client, world):
    """Attaching b to a now-global instruction would narrow 'every agent' to
    'only b'. The share is moot: no row, no badge, and an accept sent from a
    review loaded before the change is refused as stale."""
    H = _hdr(world["admin"]["token"], world["org_id"])
    iid = _instruction(test_client, world, ["a"], tables=())
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    stale_review = _review(test_client, world, iid)
    change = _scope_change(stale_review, bid)
    assert change is not None

    r = test_client.put(f"/api/instructions/{iid}", headers=H, json={"data_source_ids": []})
    assert r.status_code == 200, r.text
    assert _scope_change(_review(test_client, world, iid), bid) is None
    assert not _pending(test_client, world, iid)
    r = _resolve(test_client, world, iid, stale_review, [(bid, k) for k in change["keys"]], "accept")
    assert r.status_code == 409, r.text
    assert _live(test_client, world, iid)[0] == set()  # still global
    # And the build publishes: nothing is left unresolved, and the whole-build
    # publish does NOT silently narrow the now-global instruction back to [a,b]
    # (promotion skips a live-global instruction, like the review does).
    r = test_client.post(f"/api/builds/{bid}/publish", headers=H)
    assert r.status_code == 200, r.text
    assert _live(test_client, world, iid)[0] == set()  # still global after publish


@pytest.mark.asyncio
async def test_publishing_the_rest_of_the_build_after_accepting_the_share_keeps_the_share(test_client, world):
    """After the share is accepted, a whole-build publish of the suggestion
    applies its text and leaves the live attachment — b and its tables —
    exactly as the accept left it. (The suggestion's version snapshot predates
    the share and does not list b's tables; by design a snapshot never carries
    a proposal, so a rejected share's tables can never reach main either.)"""
    H = _hdr(world["admin"]["token"], world["org_id"])
    iid = _instruction(test_client, world, ["a"])
    bid, out = await _edit(world, {"instruction_id": iid, "old_text": "net of tax", "text": "net of VAT",
                                   "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    _accept(test_client, world, iid, bid)
    r = test_client.post(f"/api/builds/{bid}/publish", headers=H)
    assert r.status_code == 200, r.text
    assert "net of VAT" in _live_text(test_client, world, iid)
    agents, tables = _live(test_client, world, iid)
    assert agents == {world["a"]["id"], world["b"]["id"]}
    assert tables == {world["a"]["tables"]["Orders"], world["b"]["tables"]["Orders"]}


@pytest.mark.asyncio
async def test_publishing_a_multi_instruction_build_applies_the_share(test_client, world):
    """A build that stages a share on one instruction and a text edit on
    another publishes both: the share is applied with everything else, not
    singled out for a refusal."""
    H = _hdr(world["admin"]["token"], world["org_id"])
    shared = _instruction(test_client, world, ["a"], tables=())
    other = _instruction(test_client, world, ["a"], tables=())
    bid, out = await _edit(world, {"instruction_id": shared, "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    _b, out = await _edit(world, {"instruction_id": other, "old_text": "net of tax", "text": "net of fees"},
                          build_id=bid)
    assert out["success"], out
    r = test_client.post(f"/api/builds/{bid}/publish", headers=H)
    assert r.status_code == 200, r.text
    assert _live(test_client, world, shared)[0] == {world["a"]["id"], world["b"]["id"]}
    assert "net of fees" in _live_text(test_client, world, other)


@pytest.mark.asyncio
async def test_table_names_changes_tables_not_agents(test_client, world):
    """table_names sets which TABLES the instruction is about; it never changes
    which agents it is attached to. On an instruction scoped to a,
    table_names=['Invoices'] keeps it on a alone (NOT widened to b) and repoints
    its references at the matching Invoices. Sharing b is a separate, genuine
    change (add_agents), not something table_names already did."""
    iid = _instruction(test_client, world, ["a"])  # a's Orders
    bid, out = await _edit(world, {"instruction_id": iid, "table_names": ["Invoices"]})
    assert out["success"], out
    # add_agents is still a real change — table_names did not attach b.
    _b, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]}, build_id=bid)
    assert out["success"], out
    _accept(test_client, world, iid, bid)
    agents, tables = _live(test_client, world, iid)
    assert agents == {world["a"]["id"], world["b"]["id"]}  # b from add_agents only
    assert world["a"]["tables"]["Invoices"] in tables
    assert world["a"]["tables"]["Orders"] not in tables


@pytest.mark.asyncio
async def test_table_names_alone_does_not_widen_agents(test_client, world):
    """table_names with no add_agents leaves the attachment untouched."""
    iid = _instruction(test_client, world, ["a"])  # a's Orders
    bid, out = await _edit(world, {"instruction_id": iid, "table_names": ["Invoices"]})
    assert out["success"], out
    _accept(test_client, world, iid, bid)
    agents, tables = _live(test_client, world, iid)
    assert agents == {world["a"]["id"]}  # NOT widened
    assert world["a"]["tables"]["Invoices"] in tables
    assert world["a"]["tables"]["Orders"] not in tables


# --- round-5: the proposal lives with the build that made it --------------------

@pytest.mark.asyncio
async def test_a_rejected_share_does_not_come_back_in_the_next_session(test_client, world):
    """Session 1 shares with b; the reviewer rejects and publishes. Session 2
    makes a plain text edit: no share row, publish goes, and sharing with b
    again is a NEW proposal, not 'already shared'."""
    H = _hdr(world["admin"]["token"], world["org_id"])
    iid = _instruction(test_client, world, ["a"], tables=())
    bid, out = await _edit(world, {"instruction_id": iid, "old_text": "net of tax", "text": "net of VAT",
                                   "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    review = _review(test_client, world, iid)
    change = _scope_change(review, bid)
    assert _resolve(test_client, world, iid, review, [(bid, k) for k in change["keys"]], "reject").status_code == 200
    assert test_client.post(f"/api/builds/{bid}/publish", headers=H).status_code == 200

    bid2, out = await _edit(world, {"instruction_id": iid, "old_text": "net of VAT", "text": "net of VAT and fees"})
    assert out["success"], out
    assert _scope_change(_review(test_client, world, iid), bid2) is None
    assert test_client.post(f"/api/builds/{bid2}/publish", headers=H).status_code == 200
    bid3, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    assert _scope_change(_review(test_client, world, iid), bid3) is not None


@pytest.mark.asyncio
async def test_rollback_over_a_build_whose_share_was_rejected_works(test_client, world):
    H = _hdr(world["admin"]["token"], world["org_id"])
    iid = _instruction(test_client, world, ["a"], tables=())
    bid, out = await _edit(world, {"instruction_id": iid, "old_text": "net of tax", "text": "net of VAT",
                                   "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    review = _review(test_client, world, iid)
    change = _scope_change(review, bid)
    assert _resolve(test_client, world, iid, review, [(bid, k) for k in change["keys"]], "reject").status_code == 200
    assert test_client.post(f"/api/builds/{bid}/publish", headers=H).status_code == 200
    # move main again, then roll back to the published build
    bid2, out = await _edit(world, {"instruction_id": iid, "old_text": "net of VAT", "text": "net of VAT and fees"})
    assert test_client.post(f"/api/builds/{bid2}/publish", headers=H).status_code == 200
    r = test_client.post(f"/api/builds/{bid}/rollback", headers=H)
    assert r.status_code == 200, r.text
    assert _live_text(test_client, world, iid).endswith("net of VAT.") or "net of VAT" in _live_text(test_client, world, iid)
    assert "and fees" not in _live_text(test_client, world, iid)
    assert _live(test_client, world, iid)[0] == {world["a"]["id"]}


@pytest.mark.asyncio
async def test_resolve_suggestion_refuses_while_a_share_is_pending(test_client, world):
    H = _hdr(world["admin"]["token"], world["org_id"])
    iid = _instruction(test_client, world, ["a"], tables=())
    bid, out = await _edit(world, {"instruction_id": iid, "old_text": "net of tax", "text": "net of VAT",
                                   "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    live = _live_text(test_client, world, iid)
    r = test_client.post(f"/api/instructions/{iid}/resolve", headers=H,
                         json={"build_id": bid, "promote_text": live.replace("net of tax", "net of VAT"),
                               "remaining_text": live.replace("net of tax", "net of VAT")})
    assert r.status_code == 400, r.text
    assert r.json()["error_code"] == "build.share_pending_review"
    assert _scope_change(_review(test_client, world, iid), bid) is not None


@pytest.mark.asyncio
async def test_a_rejected_shares_tables_never_reach_main(test_client, world):
    H = _hdr(world["admin"]["token"], world["org_id"])
    iid = _instruction(test_client, world, ["a"])
    bid, out = await _edit(world, {"instruction_id": iid, "old_text": "net of tax", "text": "net of VAT",
                                   "add_agents": [world["b"]["name"]]})
    assert out["success"], out
    review = _review(test_client, world, iid)
    change = _scope_change(review, bid)
    assert _resolve(test_client, world, iid, review, [(bid, k) for k in change["keys"]], "reject").status_code == 200
    assert test_client.post(f"/api/builds/{bid}/publish", headers=H).status_code == 200
    from app.dependencies import async_session_maker
    from app.models.instruction import Instruction
    from app.models.instruction_version import InstructionVersion
    async with async_session_maker() as db:
        inst = await db.get(Instruction, iid)
        v = await db.get(InstructionVersion, inst.current_version_id)
    assert {str(r["object_id"]) for r in (v.references_json or [])} == {world["a"]["tables"]["Orders"]}


@pytest.mark.asyncio
async def test_accepting_a_share_whose_agents_were_all_deleted_mints_no_version(test_client, world):
    H = _hdr(world["admin"]["token"], world["org_id"])
    iid = _instruction(test_client, world, ["a"], tables=())
    bid, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["d"]["name"]]})
    assert out["success"], out
    assert test_client.delete(f"/api/data_sources/{world['d']['id']}", headers=H).status_code in (200, 204)
    from sqlalchemy import select, func
    from app.dependencies import async_session_maker
    from app.models.instruction_version import InstructionVersion
    async with async_session_maker() as db:
        before = (await db.execute(select(func.count()).where(InstructionVersion.instruction_id == iid))).scalar()
    _accept(test_client, world, iid, bid)
    async with async_session_maker() as db:
        after = (await db.execute(select(func.count()).where(InstructionVersion.instruction_id == iid))).scalar()
    assert after == before
    assert _scope_change(_review(test_client, world, iid), bid) is None
    assert not _pending(test_client, world, iid)


@pytest.mark.asyncio
async def test_a_deleted_agent_cannot_be_named_in_add_agents(test_client, world):
    # Soft-delete d at the DB level and LEAVE its conversation association in
    # place, so the only thing that can keep it out of the candidate list is
    # the tool's DataSource.deleted_at filter — not a removed association (the
    # delete endpoint drops the row, which would exclude d for either reason
    # and never exercise the filter).
    iid = _instruction(test_client, world, ["a"], tables=())
    from datetime import datetime
    from sqlalchemy import update
    from app.dependencies import async_session_maker
    from app.models.data_source import DataSource
    async with async_session_maker() as db:
        await db.execute(update(DataSource).where(DataSource.id == world["d"]["id"])
                         .values(deleted_at=datetime.utcnow()))
        await db.commit()
    _b, out = await _edit(world, {"instruction_id": iid, "add_agents": [world["d"]["name"]]})
    assert out["success"] is False
    assert out["rejected_reason"] == "agent_not_in_conversation"


@pytest.mark.asyncio
async def test_empty_add_agents_does_not_turn_a_no_op_status_into_a_version(test_client, world):
    iid = _instruction(test_client, world, ["a"], tables=())
    _b, out = await _edit(world, {"instruction_id": iid, "status": "active", "add_agents": []})
    assert out["success"] is False
    assert out["rejected_reason"] == "no_change"

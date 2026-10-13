"""E2E: an AI suggestion can turn an instruction on or off, and that change is
reviewed like any other.

`edit_instruction` takes `status: active|inactive`. Inactive is
Instruction.status='draft' — what every context loader filters out. The change
is staged as a suggestion and only reaches the live row when a reviewer accepts.

The review flow used to be text-only, so a status change was invisible to it:
no hunks meant no pending badge, the chat card read "accepted", and accepting
text hunks rebuilt the version from MAIN's metadata (dropping it). These tests
pin the contract through the public routes:

- a status suggestion is pending and reviewable (a `field_changes` entry);
- accepting it applies exactly that change; rejecting it changes nothing;
- accepting only a suggestion's text hunks leaves its status change pending;
- a text-only edit never changes status, from either starting state;
- publishing the whole AI build applies the staged status too.
"""
import uuid
from types import SimpleNamespace

import pytest


TEXT = "Revenue excludes cancelled orders. Use net amounts for all metrics."
DB_STATUS = {"active": "published", "inactive": "draft"}
OTHER = {"active": "inactive", "inactive": "active"}


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _new_admin(create_user, login_user, whoami):
    email = f"status_{uuid.uuid4().hex[:8]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    me = whoami(token)
    return token, me["id"], me["organizations"][0]["id"]


async def _session_build(org_id, user_id):
    """A fresh AI draft build of its own, as one training session gets."""
    from app.dependencies import async_session_maker
    from app.services.build_service import BuildService
    from tests.utils.real_execution import make_agent_execution

    exec_id = await make_agent_execution(org_id, user_id)
    async with async_session_maker() as db:
        build = await BuildService().get_or_create_draft_build(
            db, org_id=str(org_id), source="ai", user_id=str(user_id),
            agent_execution_id=exec_id)
        return str(build.id)


async def _edit(tool_input, *, user_id, org_id, mode="training", build_id=None, tool=None):
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        ctx = {
            "db": db,
            "user": SimpleNamespace(id=user_id),
            "organization": SimpleNamespace(id=org_id),
            "mode": mode,
        }
        if build_id:
            ctx["training_build_id"] = build_id
        end = None
        async for evt in (tool or EditInstructionTool()).run_stream(tool_input, ctx):
            if evt.type == "tool.error":
                pytest.fail(f"tool errored: {evt.payload}")
            if evt.type == "tool.end":
                end = evt
        assert end is not None
        return end.payload["output"]


def _live_status(test_client, iid, token, org_id):
    r = test_client.get(f"/api/instructions/{iid}", headers=_auth(token, org_id))
    assert r.status_code == 200, r.text
    return r.json()["status"]


def _review(test_client, iid, token, org_id):
    r = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=_auth(token, org_id))
    assert r.status_code == 200, r.text
    return r.json()


def _suggestion(review, build_id):
    return next((s for s in review["suggestions"] if s["build_id"] == build_id), None)


def _is_pending(test_client, iid, token, org_id):
    r = test_client.get("/api/instructions/pending-changes", headers=_auth(token, org_id))
    assert r.status_code == 200, r.text
    return iid in r.json()["instruction_ids"]


def _verdict(test_client, iid, bid, token, org_id):
    r = test_client.get(f"/api/instructions/{iid}/builds/{bid}/verdict",
                        headers=_auth(token, org_id))
    assert r.status_code == 200, r.text
    return r.json()["status"]


def _resolve(test_client, iid, token, org_id, review, keys, action):
    r = test_client.post(
        f"/api/instructions/{iid}/hunks/{action}-all",
        headers=_auth(token, org_id),
        json={
            "against_main_build_id": review["main_build_id"],
            "against_main_version_id": review["main_version_id"],
            "hunks": [{"build_id": bid, "hunk_key": key} for bid, key in keys],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _instruction(create_global_instruction, token, org_id, start):
    """A LIVE instruction (admin-created, so it is in the main build) in the
    given state — 'inactive' is a live row with status 'draft'."""
    instr = create_global_instruction(
        text=TEXT, user_token=token, org_id=org_id, status=DB_STATUS[start])
    return instr["id"]


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("start", ["active", "inactive"])
async def test_status_suggestion_is_pending_until_reviewed(
    start, test_client, create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    iid = _instruction(create_global_instruction, token, org_id, start)
    target = OTHER[start]

    out = await _edit({"instruction_id": iid, "status": target,
                       "evidence": "User asked to switch it."},
                      user_id=user_id, org_id=org_id)
    assert out["success"] is True, out
    assert (out["previous_status"], out["new_status"]) == (start, target)

    # Staged, not applied.
    assert _live_status(test_client, iid, token, org_id) == DB_STATUS[start]

    s = _suggestion(_review(test_client, iid, token, org_id), out["build_id"])
    assert s is not None, "a status-only suggestion must be reviewable"
    assert s["hunks"] == []
    assert [(f["field"], f["from"], f["to"]) for f in s["field_changes"]] == [
        ("status", DB_STATUS[start], DB_STATUS[target])
    ]
    assert _is_pending(test_client, iid, token, org_id)
    assert _verdict(test_client, iid, out["build_id"], token, org_id) == "pending"


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("start", ["active", "inactive"])
@pytest.mark.parametrize("action", ["accept", "reject"])
async def test_reviewing_a_status_suggestion_decides_the_live_status(
    start, action, test_client, create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    iid = _instruction(create_global_instruction, token, org_id, start)
    target = OTHER[start]
    out = await _edit({"instruction_id": iid, "status": target,
                       "evidence": "User asked to switch it."},
                      user_id=user_id, org_id=org_id)
    bid = out["build_id"]

    review = _review(test_client, iid, token, org_id)
    keys = [(bid, f["key"]) for f in _suggestion(review, bid)["field_changes"]]
    _resolve(test_client, iid, token, org_id, review, keys, action)

    expected = target if action == "accept" else start
    assert _live_status(test_client, iid, token, org_id) == DB_STATUS[expected]
    # Resolved for good: nothing left to review, no badge, a recorded verdict.
    assert _suggestion(_review(test_client, iid, token, org_id), bid) is None
    assert not _is_pending(test_client, iid, token, org_id)
    assert _verdict(test_client, iid, bid, token, org_id) == (
        "accepted" if action == "accept" else "rejected")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_accepting_text_hunks_leaves_the_status_change_pending(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    """Hunk and field changes are separate decisions: accepting the text must
    neither apply nor drop the status change riding the same suggestion."""
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    iid = _instruction(create_global_instruction, token, org_id, "active")
    bid = await _session_build(org_id, user_id)

    async def edit(tool_input):
        return await _edit(tool_input, user_id=user_id, org_id=org_id, build_id=bid)

    # One suggestion, two edits: text first, then the status — the second must
    # build on the first (same build), not discard it.
    assert (await edit({"instruction_id": iid,
                        "old_text": "Use net amounts for all metrics.",
                        "text": "Use gross amounts for all metrics.",
                        "evidence": "User clarified."}))["success"]
    assert (await edit({"instruction_id": iid, "status": "inactive",
                        "evidence": "User asked to retire it."}))["success"]

    review = _review(test_client, iid, token, org_id)
    s = _suggestion(review, bid)
    assert s["hunks"], "the text edit survived the later status edit"
    assert [f["field"] for f in s["field_changes"]] == ["status"]

    _resolve(test_client, iid, token, org_id, review,
             [(bid, h["key"]) for h in s["hunks"]], "accept")
    detail = test_client.get(f"/api/instructions/{iid}", headers=_auth(token, org_id)).json()
    assert "gross amounts" in detail["text"]
    assert detail["status"] == "published", "accepting text must not apply the status"

    review = _review(test_client, iid, token, org_id)
    s = _suggestion(review, bid)
    assert s is not None and s["hunks"] == []
    assert [f["to"] for f in s["field_changes"]] == ["draft"], "and must not drop it"
    assert _is_pending(test_client, iid, token, org_id)

    _resolve(test_client, iid, token, org_id, review,
             [(bid, f["key"]) for f in s["field_changes"]], "accept")
    assert _live_status(test_client, iid, token, org_id) == "draft"
    assert not _is_pending(test_client, iid, token, org_id)


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("start", ["active", "inactive"])
async def test_text_edit_never_changes_status(
    start, test_client, create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    iid = _instruction(create_global_instruction, token, org_id, start)

    out = await _edit({"instruction_id": iid,
                       "old_text": "Use net amounts for all metrics.",
                       "text": "Use gross amounts for all metrics.",
                       "evidence": "User clarified."},
                      user_id=user_id, org_id=org_id)
    assert out["success"] is True, out
    assert out["new_status"] is None

    review = _review(test_client, iid, token, org_id)
    s = _suggestion(review, out["build_id"])
    assert s["field_changes"] == []

    # Both ways of approving it keep the status: hunk review ...
    _resolve(test_client, iid, token, org_id, review,
             [(out["build_id"], h["key"]) for h in s["hunks"]], "accept")
    assert _live_status(test_client, iid, token, org_id) == DB_STATUS[start]


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("start", ["active", "inactive"])
@pytest.mark.parametrize("change_status", [True, False])
async def test_publishing_the_ai_build_applies_exactly_the_staged_status(
    start, change_status, test_client, create_global_instruction,
    create_user, login_user, whoami
):
    """... and whole-build publish (BuildExplorer) — which also has to APPLY a
    staged status change, in both directions."""
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    iid = _instruction(create_global_instruction, token, org_id, start)
    tool_input = {"instruction_id": iid,
                  "old_text": "Use net amounts for all metrics.",
                  "text": "Use gross amounts for all metrics.",
                  "evidence": "User clarified."}
    if change_status:
        tool_input["status"] = OTHER[start]
    out = await _edit(tool_input, user_id=user_id, org_id=org_id)
    assert out["success"] is True, out

    r = test_client.post(f"/api/builds/{out['build_id']}/publish", headers=_auth(token, org_id))
    assert r.status_code == 200, r.text

    expected = OTHER[start] if change_status else start
    assert _live_status(test_client, iid, token, org_id) == DB_STATUS[expected]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_status_edit_guards(
    create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    iid = _instruction(create_global_instruction, token, org_id, "active")

    # Already in that state: nothing to suggest.
    out = await _edit({"instruction_id": iid, "status": "active"},
                      user_id=user_id, org_id=org_id)
    assert (out["success"], out["rejected_reason"]) == (False, "no_change")

    # The knowledge harness edits autonomously — it may not switch
    # instructions on or off.
    out = await _edit({"instruction_id": iid, "status": "inactive"},
                      user_id=user_id, org_id=org_id, mode="knowledge")
    assert (out["success"], out["rejected_reason"]) == (False, "status_change_not_allowed")

    # load_mode is not an off switch, and unknown values are refused rather
    # than silently coerced to another mode.
    for bad in ("disabled", "sometimes"):
        out = await _edit({"instruction_id": iid, "load_mode": bad},
                          user_id=user_id, org_id=org_id)
        assert (out["success"], out["rejected_reason"]) == (False, "invalid_load_mode")


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("org_has_main_build", [True, False])
async def test_a_text_edit_on_a_pending_create_keeps_it_going_live(
    org_has_main_build, test_client, create_global_instruction, create_user, login_user, whoami
):
    """An AI-created instruction not yet accepted has a 'draft' row, but is
    meant to go live as published. A later text edit (another session, another
    build) must not read that row as "inactive" and stage it as such."""
    from app.ai.tools.implementations.create_instruction import CreateInstructionTool

    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    if org_has_main_build:
        # Any person-made instruction publishes the org's first main build.
        _instruction(create_global_instruction, token, org_id, "active")
    created = await _edit(
        {"text": "Exclude internal test accounts from every customer metric.",
         "category": "general", "confidence": 0.9, "evidence": "User confirmed."},
        user_id=user_id, org_id=org_id,
        build_id=await _session_build(org_id, user_id), tool=CreateInstructionTool())
    iid = created["instruction_id"]
    assert _live_status(test_client, iid, token, org_id) == "draft"

    # A different session: its build does not stage the create.
    out = await _edit({"instruction_id": iid,
                       "old_text": "every customer metric.",
                       "text": "every customer and revenue metric.",
                       "evidence": "User clarified."},
                      user_id=user_id, org_id=org_id,
                      build_id=await _session_build(org_id, user_id))
    assert out["success"] is True, out
    r = test_client.post(f"/api/builds/{out['build_id']}/publish", headers=_auth(token, org_id))
    assert r.status_code == 200, r.text
    assert _live_status(test_client, iid, token, org_id) == "published"

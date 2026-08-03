"""Cross-surface logic: act in the /agents Knowledge Explorer, observe in the
training session — and back.

Both surfaces call the same instruction/review APIs; these cases pin the
round-trip invariants a reviewer relies on. Companion coverage that already
exists elsewhere (not duplicated here):

- verdict per build (pending/accepted/rejected/unknown)      -> test_instruction_verdict_and_echo.py
- accepted/rejected session events reach the chat + context  -> test_instruction_review_events.py
- suggestion diffs stay correct while main moves             -> test_instruction_stale_base.py
- accept-all scoped by build resolves only that suggestion   -> test_instruction_edit_anchors.py / UI

Case list (this file):
  T1  accept ONE suggestion in /agents -> that build reads 'accepted' back in
      the session card; the sibling suggestion stays 'pending' untouched.
  T2  reject a suggestion in /agents -> session card reads 'rejected'; live
      text unchanged.
  T3  a human EDIT in /agents while a session suggestion is pending -> the
      pending suggestion's review recomputes against the new live text (no
      ghost re-additions of the human's line, suggestion's own line intact).
  T4  after an /agents accept, the session's read_instruction returns the new
      LIVE text and no longer lists the accepted suggestion as pending.
"""
import uuid
from types import SimpleNamespace

import pytest


ORIGINAL = "Revenue excludes cancelled orders.\n- Use net amounts for all metrics."


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _new_admin(create_user, login_user, whoami):
    email = f"xsurf_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    me = whoami(token)
    return token, me["id"], me["organizations"][0]["id"]


async def _stage(iid, org_id, user_id, *, old, new):
    """Stage one suggestion (its own execution => its own build), as a
    training-session turn would. The execution is a real row — the build's
    agent_execution_id FK is enforced on Postgres."""
    from app.dependencies import async_session_maker
    from app.services.build_service import BuildService
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool
    from tests.utils.real_execution import make_agent_execution

    exec_id = await make_agent_execution(org_id, user_id)
    async with async_session_maker() as db:
        build = await BuildService().get_or_create_draft_build(
            db, org_id=str(org_id), source="ai", user_id=str(user_id),
            agent_execution_id=exec_id)
        ctx = {"db": db, "user": SimpleNamespace(id=user_id),
               "organization": SimpleNamespace(id=org_id), "mode": "training",
               "training_build_id": str(build.id), "agent_execution_id": exec_id}
        out = None
        async for evt in EditInstructionTool().run_stream(
            {"instruction_id": iid, "old_text": old, "text": new,
             "evidence": "User asked."}, ctx):
            if evt.type == "tool.error":
                pytest.fail(f"tool errored: {evt.payload}")
            if evt.type == "tool.end":
                out = evt.payload["output"]
        assert out["success"] is True, out
        return out["build_id"]


def _verdict(test_client, iid, bid, token, org_id):
    r = test_client.get(f"/api/instructions/{iid}/builds/{bid}/verdict",
                        headers=_hdr(token, org_id))
    assert r.status_code == 200, r.text
    return r.json()["status"]


def _review(test_client, iid, token, org_id):
    r = test_client.get(f"/api/instructions/{iid}/review-hunks", headers=_hdr(token, org_id))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_T1_accept_one_suggestion_leaves_sibling_pending(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    token, uid, org = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(text=ORIGINAL, user_token=token, org_id=org, status="published")
    iid = instr["id"]

    bid_a = await _stage(iid, org, uid,
                         old="- Use net amounts for all metrics.",
                         new="- Use net amounts (excluding VAT) for all metrics.")
    bid_b = await _stage(iid, org, uid,
                         old="Revenue excludes cancelled orders.",
                         new="Revenue excludes cancelled and refunded orders.")
    assert bid_a != bid_b

    # /agents page: accept ONLY suggestion A (build-scoped accept-all).
    review = _review(test_client, iid, token, org)
    r = test_client.post(
        f"/api/instructions/{iid}/hunks/accept-all",
        headers=_hdr(token, org),
        json={"against_main_version_id": review["main_version_id"], "build_id": bid_a},
    )
    assert r.status_code == 200, r.text

    # Back in the session: A reads accepted, B still pending.
    assert _verdict(test_client, iid, bid_a, token, org) == "accepted"
    assert _verdict(test_client, iid, bid_b, token, org) == "pending"

    live = _review(test_client, iid, token, org)["main_text"]
    assert "excluding VAT" in live
    assert "refunded" not in live, "sibling suggestion must not have been applied"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_T2_reject_reads_back_rejected_and_live_text_untouched(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    token, uid, org = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(text=ORIGINAL, user_token=token, org_id=org, status="published")
    iid = instr["id"]
    bid = await _stage(iid, org, uid,
                       old="- Use net amounts for all metrics.",
                       new="- Use gross amounts for all metrics.")

    r = test_client.post(f"/api/instructions/{iid}/hunks/reject-all",
                         headers=_hdr(token, org), json={"build_id": bid})
    assert r.status_code == 200, r.text

    assert _verdict(test_client, iid, bid, token, org) == "rejected"
    live = _review(test_client, iid, token, org)["main_text"]
    assert "gross" not in live and "net amounts" in live


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_T3_human_edit_in_agents_page_recomputes_pending_review(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    token, uid, org = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(text=ORIGINAL, user_token=token, org_id=org, status="published")
    iid = instr["id"]
    bid = await _stage(iid, org, uid,
                       old="- Use net amounts for all metrics.",
                       new="- Use net amounts (excluding VAT) for all metrics.")

    # Human edits directly in /agents while the suggestion is pending.
    human_line = "\n- Attribute sales to reps via the support rep foreign key."
    r = test_client.put(f"/api/instructions/{iid}", headers=_hdr(token, org),
                        json={"text": ORIGINAL + human_line})
    assert r.status_code == 200, r.text

    review = _review(test_client, iid, token, org)
    assert _verdict(test_client, iid, bid, token, org) == "pending"
    added = " ".join(
        (h.get("after") or "") for s in review["suggestions"] for h in s["hunks"]
    )
    # The suggestion still proposes ITS change...
    assert "excluding VAT" in added
    # ...and does NOT re-propose the human's already-live line as new.
    assert "support rep foreign key" not in added


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_T4_session_read_reflects_agents_page_accept(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    token, uid, org = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(text=ORIGINAL, user_token=token, org_id=org, status="published")
    iid = instr["id"]
    bid = await _stage(iid, org, uid,
                       old="- Use net amounts for all metrics.",
                       new="- Use net amounts (excluding VAT) for all metrics.")

    review = _review(test_client, iid, token, org)
    r = test_client.post(
        f"/api/instructions/{iid}/hunks/accept-all", headers=_hdr(token, org),
        json={"against_main_version_id": review["main_version_id"], "build_id": bid},
    )
    assert r.status_code == 200, r.text

    # The session's read tool now sees the accepted text as LIVE, with no
    # pending change left for that suggestion.
    from app.dependencies import async_session_maker
    from app.ai.tools.implementations.read_instruction import ReadInstructionTool
    async with async_session_maker() as db:
        ctx = {"db": db, "user": SimpleNamespace(id=uid),
               "organization": SimpleNamespace(id=org), "mode": "training",
               "report": SimpleNamespace(id=str(uuid.uuid4()), data_sources=[])}
        out = None
        async for evt in ReadInstructionTool().run_stream({"id": iid[:8]}, ctx):
            if evt.type == "tool.end":
                out = evt.payload["output"]
    assert out["success"] is True, out
    assert "excluding VAT" in out["text"]
    pend = out.get("pending_changes") or []
    assert not any(str(p.get("build_id")) == str(bid) for p in pend), (
        "an accepted suggestion must not still read as pending"
    )

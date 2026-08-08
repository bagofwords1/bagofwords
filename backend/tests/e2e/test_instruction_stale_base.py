"""E2E: a suggestion's review must stay correct when main moves under it.

A build records where it FORKED (``InstructionBuild.base_build_id``), once, at
creation — and nothing ever rebases it. Main, meanwhile, advances every time
anyone accepts a hunk, edits an instruction directly, or runs a git sync. Diffing
fork-point -> proposed then credits the suggestion with everything main gained in
between, which renders the instruction's own live text as green additions and,
on accept, appends a duplicate copy of it.

``BuildContent.base_version_id`` fixes that by recording the baseline per
(build, instruction) at the moment the build first changed it. Contract:

1. a suggestion staged after main moved shows ONLY its own changes,
2. accepting it does not duplicate text that is already live,
3. the baseline is stamped once — a second edit in the same build keeps the
   first edit's baseline, so review shows the accumulated change, not just the
   latest tweak,
4. rows with no stamp (written before the column existed) still resolve through
   the old fork-point path.
"""
import uuid
from types import SimpleNamespace

import pytest


ORIGINAL = (
    "Revenue excludes cancelled orders.\n"
    "- Use net amounts for all revenue metrics."
)
# What a user adds directly while the AI's session is open.
ADDED_BY_USER = "\n- Attribute sales to reps via the support rep foreign key."


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _new_admin(create_user, login_user, whoami):
    email = f"stale_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    me = whoami(token)
    return token, me["id"], me["organizations"][0]["id"]


async def _open_ai_draft(org_id, user_id):
    """The draft build the edit_instruction tool would lazily create, bound to
    a real agent execution (the FK is enforced on Postgres)."""
    from app.dependencies import async_session_maker
    from app.services.build_service import BuildService
    from tests.utils.real_execution import make_agent_execution

    exec_id = await make_agent_execution(org_id, user_id)
    async with async_session_maker() as db:
        build = await BuildService().get_or_create_draft_build(
            db, org_id=str(org_id), source="ai", user_id=str(user_id),
            agent_execution_id=exec_id,
        )
        return str(build.id)


async def _stage_ai_edit(instruction_id, build_id, org_id, user_id, *, text,
                         old_text=None, mode="training",
                         replace_entire_text=False):
    from app.dependencies import async_session_maker
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool

    tool_input = {"instruction_id": instruction_id, "text": text,
                  "evidence": "User confirmed the rule."}
    if old_text is not None:
        tool_input["old_text"] = old_text
    if replace_entire_text:
        # These cases deliberately model a whole-instruction rewrite, which is
        # now an explicit act rather than an omitted argument.
        tool_input["replace_entire_text"] = True

    async with async_session_maker() as db:
        ctx = {
            "db": db,
            "user": SimpleNamespace(id=user_id),
            "organization": SimpleNamespace(id=org_id),
            "mode": mode,
            "training_build_id": build_id,
        }
        end = None
        async for evt in EditInstructionTool().run_stream(tool_input, ctx):
            if evt.type == "tool.error":
                pytest.fail(f"tool errored: {evt.payload}")
            if evt.type == "tool.end":
                end = evt
        assert end is not None
        return end.payload["output"]


def _review(test_client, iid, token, org_id):
    r = test_client.get(f"/api/instructions/{iid}/review-hunks",
                        headers=_auth(token, org_id))
    assert r.status_code == 200, r.text
    return r.json()


def _added_lines(review):
    """Every non-blank line the review renders as an addition."""
    out = []
    for s in review["suggestions"]:
        for h in s["hunks"]:
            out += [ln.strip() for ln in (h.get("after") or "").splitlines() if ln.strip()]
    return out


def _advance_main(test_client, iid, token, org_id, new_text):
    """A direct user edit — creates a version, builds it, promotes to main."""
    r = test_client.put(f"/api/instructions/{iid}",
                        headers=_auth(token, org_id), json={"text": new_text})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_suggestion_staged_after_main_moved_shows_only_its_own_change(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published")
    iid = instr["id"]

    # Session opens: the draft forks from main as it looks right now.
    build_id = await _open_ai_draft(org_id, user_id)

    # Someone edits the instruction outside the session — main moves on.
    _advance_main(test_client, iid, token, org_id, ORIGINAL + ADDED_BY_USER)

    # The AI now stages its edit, written (as the tool always does) against the
    # CURRENT live text — so its proposal contains the user's line too.
    proposed = ORIGINAL + ADDED_BY_USER + "\n- Refunded orders are excluded as well."
    out = await _stage_ai_edit(iid, build_id, org_id, user_id, text=proposed,
                               replace_entire_text=True)
    assert out["success"] is True, out

    review = _review(test_client, iid, token, org_id)
    live_lines = {ln.strip() for ln in review["main_text"].splitlines() if ln.strip()}
    added = _added_lines(review)

    assert added, "the AI's own change must still surface for review"
    # The whole point: nothing already live may be presented as an addition.
    ghosts = [ln for ln in added if ln in live_lines]
    assert not ghosts, f"live text shown as new additions: {ghosts}"
    assert any("Refunded orders" in ln for ln in added)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_accepting_that_suggestion_does_not_duplicate_live_text(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published")
    iid = instr["id"]

    build_id = await _open_ai_draft(org_id, user_id)
    _advance_main(test_client, iid, token, org_id, ORIGINAL + ADDED_BY_USER)
    proposed = ORIGINAL + ADDED_BY_USER + "\n- Refunded orders are excluded as well."
    await _stage_ai_edit(iid, build_id, org_id, user_id, text=proposed,
                         replace_entire_text=True)

    review = _review(test_client, iid, token, org_id)
    r = test_client.post(
        f"/api/instructions/{iid}/hunks/accept-all",
        headers=_auth(token, org_id),
        json={
            "against_main_build_id": review["main_build_id"],
            "against_main_version_id": review["main_version_id"],
            "hunks": [
                {"build_id": s["build_id"], "hunk_key": h["key"]}
                for s in review["suggestions"] for h in s["hunks"]
            ],
        },
    )
    assert r.status_code == 200, r.text

    final = _review(test_client, iid, token, org_id)["main_text"]
    bullets = [ln.strip() for ln in final.splitlines() if ln.strip().startswith("-")]
    assert len(bullets) == len(set(bullets)), f"accept duplicated bullets: {bullets}"
    # The user's line survives and the AI's line landed exactly once.
    assert sum("support rep foreign key" in b for b in bullets) == 1
    assert sum("Refunded orders" in b for b in bullets) == 1


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_second_edit_in_same_build_keeps_the_first_baseline(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    """The stamp is written once. A build that edits an instruction twice must
    show the accumulated change against where it started — not just the last
    tweak, which is what a per-edit baseline would (wrongly) surface."""
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published")
    iid = instr["id"]
    build_id = await _open_ai_draft(org_id, user_id)

    # Anchored appends — "" is not an anchor. The second anchors on the first
    # addition, which also proves it based itself on the PENDING version.
    await _stage_ai_edit(iid, build_id, org_id, user_id, old_text=ORIGINAL,
                         text=f"{ORIGINAL}\n- First addition.")
    await _stage_ai_edit(iid, build_id, org_id, user_id,
                         old_text="- First addition.",
                         text="- First addition.\n- Second addition.")

    added = " ".join(_added_lines(_review(test_client, iid, token, org_id)))
    assert "First addition." in added, "the earlier edit must stay visible"
    assert "Second addition." in added

    from app.dependencies import async_session_maker
    from sqlalchemy import select
    from app.models.build_content import BuildContent
    async with async_session_maker() as db:
        stamped = (await db.execute(
            select(BuildContent.base_version_id).where(
                BuildContent.build_id == build_id,
                BuildContent.instruction_id == iid,
            )
        )).scalar()
    assert stamped is not None, "the first change must stamp a baseline"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_unstamped_row_falls_back_to_fork_point(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    """Rows written before base_version_id existed must keep resolving through
    base_build_id — the migration adds a nullable column and backfills nothing."""
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published")
    iid = instr["id"]
    build_id = await _open_ai_draft(org_id, user_id)
    await _stage_ai_edit(iid, build_id, org_id, user_id, old_text=ORIGINAL,
                         text=f"{ORIGINAL}\n- Refunded orders are excluded as well.")

    # Simulate a pre-migration row.
    from app.dependencies import async_session_maker
    from sqlalchemy import update
    from app.models.build_content import BuildContent
    async with async_session_maker() as db:
        await db.execute(
            update(BuildContent)
            .where(BuildContent.build_id == build_id,
                   BuildContent.instruction_id == iid)
            .values(base_version_id=None)
        )
        await db.commit()

    added = " ".join(_added_lines(_review(test_client, iid, token, org_id)))
    assert "Refunded orders" in added, "legacy rows must still review correctly"

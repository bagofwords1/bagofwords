"""E2E tests for edit_instruction anchor semantics (anti-destructive edits).

The knowledge harness edits instructions autonomously, so a text edit must be
surgical: ``old_text`` is always an exact snippet of the current text, and
adding means anchoring the sentence the addition follows and repeating it.
Neither ``old_text: ""`` nor omitting ``old_text`` is an anchor — both are
rejected. Whole-text replacement needs an explicit ``replace_entire_text:
true``, and stays rejected in knowledge mode even then.
Contract under test:

1. an anchored append preserves the existing text verbatim and adds to it
   (``old_text: \"\"`` is NOT an anchor and is rejected),
2. an anchored replace changes only the anchored snippet (whitespace
   differences in the anchor do not fail the match),
3. a missing anchor is rejected with the current text surfaced for retry,
   and nothing is staged,
4. an ambiguous anchor (multiple matches) is rejected,
5. omitting the anchor is rejected in BOTH modes; an explicit
   ``replace_entire_text`` is allowed in training and refused in knowledge,
6. live instruction text never changes before promotion (edits are staged).

The generality-gate LLM is absent in these tests, so the gate fails open —
gate behavior itself is covered in test_instruction_overfit.py.
"""
import uuid
from types import SimpleNamespace

import pytest


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _new_admin(create_user, login_user, whoami):
    email = f"anchor_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    me = whoami(token)
    return token, me["id"], me["organizations"][0]["id"]


async def _run_edit(tool_input, *, user_id, org_id, mode="knowledge"):
    from app.dependencies import async_session_maker
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool

    async with async_session_maker() as db:
        ctx = {
            "db": db,
            "user": SimpleNamespace(id=user_id),
            "organization": SimpleNamespace(id=org_id),
            "mode": mode,
        }
        end = None
        async for evt in EditInstructionTool().run_stream(tool_input, ctx):
            if evt.type == "tool.error":
                pytest.fail(f"tool errored: {evt.payload}")
            if evt.type == "tool.end":
                end = evt
        assert end is not None, "expected a tool.end event"
        return end.payload["output"], end.payload.get("observation") or {}


def _ai_suggestions(test_client, iid, token, org_id):
    review = test_client.get(
        f"/api/instructions/{iid}/review-hunks", headers=_auth(token, org_id)
    ).json()
    return [s for s in review["suggestions"] if s["source"] == "ai"]


ORIGINAL = "Revenue excludes cancelled orders. Use net amounts for all revenue metrics."


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_append_preserves_existing_text(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published"
    )

    addition = "Refunded orders are excluded from revenue as well."
    # Anchored append: `old_text` must be real text ("" is not an anchor), so
    # anchor the tail and repeat it verbatim ahead of the addition.
    output, _ = await _run_edit(
        {"instruction_id": instr["id"],
         "old_text": "Use net amounts for all revenue metrics.",
         "text": f"Use net amounts for all revenue metrics. {addition}",
         "evidence": "User clarified: refunds never count."},
        user_id=user_id, org_id=org_id,
    )
    assert output["success"] is True, output
    # Existing content survives verbatim; the addition lands after it.
    assert output["new_text"].startswith(ORIGINAL)
    assert addition in output["new_text"]
    assert output["previous_text"] == ORIGINAL

    # Edit is staged as an AI suggestion; live row untouched until promotion.
    assert _ai_suggestions(test_client, instr["id"], token, org_id)
    live = test_client.get(
        f"/api/instructions/{instr['id']}", headers=_auth(token, org_id)
    ).json()
    assert live["text"] == ORIGINAL


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_anchored_replace_changes_only_the_snippet(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published"
    )

    output, _ = await _run_edit(
        {
            "instruction_id": instr["id"],
            "old_text": "excludes cancelled orders",
            "text": "excludes cancelled and refunded orders",
            "evidence": "User corrected: refunded orders excluded too.",
        },
        user_id=user_id, org_id=org_id,
    )
    assert output["success"] is True, output
    assert output["new_text"] == (
        "Revenue excludes cancelled and refunded orders. "
        "Use net amounts for all revenue metrics."
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_anchor_matches_across_whitespace_differences(
    create_global_instruction, create_user, login_user, whoami
):
    """Instructions are prose; a spacing/newline mismatch in the anchor must
    not fail the edit."""
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text="Revenue excludes\n  cancelled   orders. Use net amounts.",
        user_token=token, org_id=org_id, status="published",
    )

    output, _ = await _run_edit(
        {
            "instruction_id": instr["id"],
            "old_text": "Revenue excludes cancelled orders.",
            "text": "Revenue excludes cancelled and refunded orders.",
        },
        user_id=user_id, org_id=org_id,
    )
    assert output["success"] is True, output
    assert "cancelled and refunded" in output["new_text"]
    assert "Use net amounts." in output["new_text"]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_missing_anchor_rejected_with_current_text_and_nothing_staged(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published"
    )

    output, observation = await _run_edit(
        {
            "instruction_id": instr["id"],
            "old_text": "this snippet does not exist anywhere",
            "text": "replacement",
        },
        user_id=user_id, org_id=org_id,
    )
    assert output["success"] is False
    assert output["rejected_reason"] == "anchor_not_found"
    # The current text is surfaced so the planner's retry can anchor on it.
    assert observation.get("current_text") == ORIGINAL
    assert _ai_suggestions(test_client, instr["id"], token, org_id) == []


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_ambiguous_anchor_rejected(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text="Use net revenue. Always report net revenue in dashboards.",
        user_token=token, org_id=org_id, status="published",
    )

    output, _ = await _run_edit(
        {
            "instruction_id": instr["id"],
            "old_text": "net revenue",
            "text": "gross revenue",
        },
        user_id=user_id, org_id=org_id,
    )
    assert output["success"] is False
    assert output["rejected_reason"] == "ambiguous_anchor"
    assert _ai_suggestions(test_client, instr["id"], token, org_id) == []


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_full_replace_rejected_in_knowledge_mode_allowed_in_training(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published"
    )
    rewrite = "Completely new instruction text that drops everything else."

    # Omitting old_text is no longer a way to rewrite anything, in ANY mode —
    # the destructive path must not be the one with the fewest arguments. See
    # test_instruction_anchor_required.py.
    for mode in ("knowledge", "training"):
        output, observation = await _run_edit(
            {"instruction_id": instr["id"], "text": rewrite},
            user_id=user_id, org_id=org_id, mode=mode,
        )
        assert output["success"] is False, mode
        assert output["rejected_reason"] == "anchor_required", (mode, output)
        assert observation.get("current_text") == ORIGINAL
    assert _ai_suggestions(test_client, instr["id"], token, org_id) == []

    # Knowledge mode (autonomous harness): refused even when asked deliberately.
    output, observation = await _run_edit(
        {"instruction_id": instr["id"], "text": rewrite, "replace_entire_text": True},
        user_id=user_id, org_id=org_id, mode="knowledge",
    )
    assert output["success"] is False
    assert output["rejected_reason"] == "full_replace_not_allowed"
    assert observation.get("current_text") == ORIGINAL
    assert _ai_suggestions(test_client, instr["id"], token, org_id) == []

    # Training mode (human in the loop): a deliberate rewrite still works.
    output, _ = await _run_edit(
        {"instruction_id": instr["id"], "text": rewrite, "replace_entire_text": True},
        user_id=user_id, org_id=org_id, mode="training",
    )
    assert output["success"] is True, output
    assert output["new_text"] == rewrite


async def _run_edits_sequential(tool_inputs, *, user_id, org_id, mode="knowledge"):
    """Mimic the knowledge harness: a single runtime_ctx whose training_build_id
    is carried across tool calls (agent_v2 captures it back after each call), so
    every edit lands in the SAME draft build.
    """
    from app.dependencies import async_session_maker
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool

    outputs = []
    training_build_id = None
    for tool_input in tool_inputs:
        async with async_session_maker() as db:
            ctx = {
                "db": db,
                "user": SimpleNamespace(id=user_id),
                "organization": SimpleNamespace(id=org_id),
                "mode": mode,
                "training_build_id": training_build_id,
            }
            end = None
            async for evt in EditInstructionTool().run_stream(tool_input, ctx):
                if evt.type == "tool.error":
                    pytest.fail(f"tool errored: {evt.payload}")
                if evt.type == "tool.end":
                    end = evt
            assert end is not None, "expected a tool.end event"
            training_build_id = ctx.get("training_build_id") or training_build_id
            outputs.append(end.payload["output"])
    return outputs, training_build_id


async def _dump_build_state(build_id, instruction_id):
    """Return (staged_text, version_number, total_versions) for the version the
    build currently points at — i.e. what promotion would actually apply."""
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.build_content import BuildContent
    from app.models.instruction_version import InstructionVersion

    async with async_session_maker() as db:
        bc = (await db.execute(
            select(BuildContent).where(
                BuildContent.build_id == build_id,
                BuildContent.instruction_id == instruction_id,
            )
        )).scalars().all()
        versions = (await db.execute(
            select(InstructionVersion)
            .where(InstructionVersion.instruction_id == instruction_id)
            .order_by(InstructionVersion.version_number)
        )).scalars().all()
        assert len(bc) == 1, f"expected 1 BuildContent row, got {len(bc)}"
        pointed = next(
            (v for v in versions if str(v.id) == str(bc[0].instruction_version_id)), None
        )
        assert pointed is not None, "build points at a missing version"
        print(f"\n[DB] BuildContent rows for instruction: {len(bc)}")
        print(f"[DB] InstructionVersion rows: {len(versions)} "
              f"(v{[v.version_number for v in versions]})")
        print(f"[DB] build points at v{pointed.version_number}")
        for v in versions:
            mark = " <-- BUILD POINTS HERE" if str(v.id) == str(bc[0].instruction_version_id) else ""
            print(f"[DB]   v{v.version_number}: {v.text!r}{mark}")
        return pointed.text, pointed.version_number, len(versions)


LEARNING_A = "Cumulative event charts must use a running total, not per-day counts."
LEARNING_B = "Event type labels come from the event_type column, not event_name."


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_sequential_edits_in_one_build_accumulate(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    """Two appends to the same instruction within ONE harness run must BOTH
    survive promotion.

    Regression: each edit used to re-read the live row as its base (the live row
    is intentionally never mutated before approval), so v3 was built from the
    ORIGINAL text and did not contain learning A. add_to_build then repointed the
    single BuildContent row from v2 to v3, silently discarding learning A.
    """
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published"
    )

    outputs, build_id = await _run_edits_sequential(
        [
            {"instruction_id": instr["id"], "old_text": ORIGINAL,
             "text": f"{ORIGINAL}\n{LEARNING_A}",
             "evidence": "Session: user asked for cumulative charts."},
            # The second edit anchors on learning A — proving it based itself
            # on the PENDING version, not the untouched live row.
            {"instruction_id": instr["id"], "old_text": LEARNING_A,
             "text": f"{LEARNING_A}\n{LEARNING_B}",
             "evidence": "Session: user clarified label source."},
        ],
        user_id=user_id, org_id=org_id,
    )
    assert all(o["success"] is True for o in outputs), outputs

    staged_text, version_number, _ = await _dump_build_state(build_id, instr["id"])

    # The second edit must build on the first, not on the stale live row.
    assert LEARNING_A in outputs[1]["previous_text"], (
        "second edit based itself on the live row, losing the first edit"
    )

    # What promotion would actually apply must contain BOTH learnings.
    assert ORIGINAL in staged_text
    assert LEARNING_A in staged_text, "learning A was silently discarded"
    assert LEARNING_B in staged_text

    # Live row still untouched — approval gate is preserved.
    live = test_client.get(
        f"/api/instructions/{instr['id']}", headers=_auth(token, org_id)
    ).json()
    assert live["text"] == ORIGINAL

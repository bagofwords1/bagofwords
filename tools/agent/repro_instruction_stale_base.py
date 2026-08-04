"""Repro: instruction review shows text ALREADY LIVE as new green additions.

Story (matches the reported screenshots):
  1. Instruction exists on main with a body of bullets  -> main build M0.
  2. A training session starts; the AI's draft build D forks from M0.
  3. Main then advances (user accepts a hunk / edits the instruction) to M1,
     which ADDS more bullets. D.base_build_id still points at M0 and is never
     rebased.
  4. The AI stages another edit in the SAME draft D (training mode allows a
     full rewrite), restating M1's bullets with a small wording change.
  5. GET /instructions/{id}/review-hunks diffs D.base(M0) -> proposed, so every
     bullet main gained in step 3 is part of the "intent" and surfaces as a
     brand-new addition, even though it is already the live text.

Run from backend/:
  BOW_DATABASE_URL='sqlite:///db/agent.db' uv run python ../tools/agent/repro_instruction_stale_base.py
"""
import asyncio
import os
import sys
from types import SimpleNamespace

# realpath, not "<dir>/../../backend": settings.load() rejects any path
# containing ".." with a path-traversal guard.
sys.path.insert(0, os.path.realpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "backend")
))
os.environ.setdefault("BOW_DATABASE_URL", "sqlite:///db/agent.db")

import main  # noqa: F401 — registers every SQLAlchemy mapper


# --- the three texts -------------------------------------------------------
# M0: what main looked like when the training session opened.
TEXT_M0 = (
    "Use this source for procurement-domain analysis covering purchases, suppliers, "
    "contracts, requisitions, purchase orders, invoices, costs, approval statuses, "
    "and delivery details.\n\n"
    "- No database tables, columns, primary keys, foreign keys, or date/time fields are "
    "exposed in the provided schema. Do not assume table names, infer joins, invent "
    "identifiers, or state status-code meanings until source files establish them."
)

# M1: main after the user accepted more content mid-session. THIS IS LIVE TRUTH.
TEXT_M1 = TEXT_M0 + (
    "\n- No table-level reference can currently be provided because the schema lists no "
    "tables. When files become available, document each discovered table as @TableName and "
    "validate its business meaning from its contents or metadata.\n"
    "- Before answering questions requiring records or metrics, use @search_files to find "
    "relevant files by procurement topic. Use @list_files to browse the available directory "
    "structure when the topic is unclear.\n"
    "- Use @read_file to inspect file contents and metadata. For large files, read targeted "
    "sections with offset and length rather than assuming a full-file scan.\n"
    "- Establish relationships only when files explicitly show matching keys, documented "
    "foreign keys, or reliable field-level evidence. Record the exact join fields and join "
    "cardinality when identified."
)

# What the model proposes: M1 restated with "PDF" inserted + the one new rule
# the user actually asked for. A full rewrite, which training mode allows.
TEXT_PROPOSED = TEXT_M1.replace(
    "relevant files by procurement topic", "relevant PDF files by procurement topic"
).replace(
    "to inspect file contents and metadata", "to inspect PDF file contents and metadata"
).replace(
    "- No table-level reference",
    "- Process only PDF files; ignore all other file types and formats.\n- No table-level reference",
)


async def amain():
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.instruction import Instruction
    from app.models.instruction_build import InstructionBuild
    from app.services.instruction_service import InstructionService
    from app.services.build_service import BuildService
    from app.services.instruction_version_service import InstructionVersionService
    from app.schemas.instruction_schema import InstructionCreate
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool

    isvc, bsvc, vsvc = InstructionService(), BuildService(), InstructionVersionService()

    async with async_session_maker() as db:
        org = (await db.execute(select(Organization).limit(1))).scalars().first()
        user = (await db.execute(
            select(User).where(User.email == "admin@example.com")
        )).scalars().first()
        perms = await isvc._get_user_permissions(db, user, org)
        print(f"org={org.id} user={user.email}")

        # --- step 1: instruction live on main with TEXT_M0 -----------------
        created = await isvc.create_instruction(
            db,
            InstructionCreate(text=TEXT_M0, category="general", status="published"),
            user, org, force_global=True,
        )
        iid = str(created.id)
        instruction = (await db.execute(
            select(Instruction).where(Instruction.id == iid)
        )).scalars().first()
        m0_text, m0_vid, _ = await isvc._main_text_of(db, instruction)
        print(f"\n[1] instruction {iid} on main, main_len={len(m0_text)}")

        main_build = await bsvc.get_main_build(db, str(org.id))
        print(f"    main build at session start = {main_build.id if main_build else None}")

        # --- step 2: training session opens its AI draft build -------------
        # Same call the edit_instruction tool makes (source='ai' + execution id).
        draft = await bsvc.get_or_create_draft_build(
            db, org_id=str(org.id), source="ai", user_id=str(user.id),
            agent_execution_id="repro-exec-0001",
        )
        print(f"\n[2] AI draft build {draft.id}  base_build_id={draft.base_build_id}")
        assert str(draft.base_build_id) == str(main_build.id), "draft should fork from main"

        # --- step 3: main advances while the session is still open ---------
        v1 = await vsvc.create_version_from_data(
            db, instruction_id=iid, text=TEXT_M1, title=created.title,
            description=None, structured_data=None, formatted_content=None,
            status="published", load_mode="always", references_json=None,
            data_source_ids=None, label_ids=None, category_ids=None, user_id=user.id,
        )
        m1_build = await bsvc.create_build(
            db, str(org.id), source="user", user_id=str(user.id), copy_from_main=True
        )
        await bsvc.add_to_build(db, m1_build.id, iid, v1.id)
        await db.commit()
        await isvc._auto_finalize_build(db, m1_build, user, perms, trigger_reliability=False)

        await db.refresh(instruction)
        live_text, live_vid, _ = await isvc._main_text_of(db, instruction)
        new_main = await bsvc.get_main_build(db, str(org.id))
        print(f"\n[3] main advanced to build {new_main.id}, live_len={len(live_text)}")
        print(f"    live text == TEXT_M1 ? {live_text.strip() == TEXT_M1.strip()}")

        await db.refresh(draft)
        print(f"    draft.base_build_id is STILL {draft.base_build_id} "
              f"(current main is {new_main.id}) -> stale: "
              f"{str(draft.base_build_id) != str(new_main.id)}")

        # --- step 4: AI stages a full rewrite in that same draft -----------
        ctx = {
            "db": db, "user": user, "organization": org, "mode": "training",
            "training_build_id": str(draft.id), "agent_execution_id": "repro-exec-0001",
        }
        end = None
        async for evt in EditInstructionTool().run_stream(
            {"instruction_id": iid, "text": TEXT_PROPOSED,
             "evidence": "User asked: handle only PDF files."}, ctx,
        ):
            if evt.type == "tool.error":
                raise SystemExit(f"tool error: {evt.payload}")
            if evt.type == "tool.end":
                end = evt
        out = end.payload["output"]
        print(f"\n[4] edit staged: success={out['success']} v{out.get('version_number')}")

        # --- step 5: what does review say? ---------------------------------
        review = await isvc.review_hunks(db, iid, organization=org, current_user=user)
        sugg = review["suggestions"] if review else []
        print(f"\n[5] review-hunks -> {len(sugg)} suggestion(s)")

        live_now, _, _ = await isvc._main_text_of(db, instruction)
        live_lines = {ln.strip() for ln in live_now.splitlines() if ln.strip()}

        total = ghost_lines = new_lines = 0
        for s in sugg:
            for h in s["hunks"]:
                total += 1
                print(f"\n  hunk {total}: before={(h.get('before') or '')!r}")
                print("  --- rendered as GREEN ADDITION in the review UI ---")
                for ln in (h.get("after") or "").splitlines():
                    if not ln.strip():
                        continue
                    ghost = ln.strip() in live_lines
                    if ghost:
                        ghost_lines += 1
                    else:
                        new_lines += 1
                    mark = "ALREADY LIVE" if ghost else "genuinely new"
                    print(f"    [{mark:>13}] {ln.strip()[:95]}")

        print("\n" + "=" * 70)
        print(f"SCENARIO A (stale base): {total} hunk(s); of the lines shown as "
              f"additions, {ghost_lines} are ALREADY in the live instruction, "
              f"{new_lines} are new")
        print("=" * 70)

        # --- control: identical rewrite, but a draft forked from CURRENT main.
        # If this is clean, the full rewrite isn't the cause — the stale base is.
        ctrl_instr = await isvc.create_instruction(
            db,
            InstructionCreate(text=TEXT_M1, category="general", status="published"),
            user, org, force_global=True,
        )
        ctrl_iid = str(ctrl_instr.id)
        ctrl_draft = await bsvc.get_or_create_draft_build(
            db, org_id=str(org.id), source="ai", user_id=str(user.id),
            agent_execution_id="repro-exec-0002",
        )
        ctrl_ctx = {
            "db": db, "user": user, "organization": org, "mode": "training",
            "training_build_id": str(ctrl_draft.id),
            "agent_execution_id": "repro-exec-0002",
        }
        async for evt in EditInstructionTool().run_stream(
            {"instruction_id": ctrl_iid, "text": TEXT_PROPOSED,
             "evidence": "User asked: handle only PDF files."}, ctrl_ctx,
        ):
            if evt.type == "tool.error":
                raise SystemExit(f"control tool error: {evt.payload}")

        ctrl_obj = (await db.execute(
            select(Instruction).where(Instruction.id == ctrl_iid)
        )).scalars().first()
        ctrl_live, _, _ = await isvc._main_text_of(db, ctrl_obj)
        ctrl_live_lines = {l.strip() for l in ctrl_live.splitlines() if l.strip()}
        ctrl_review = await isvc.review_hunks(
            db, ctrl_iid, organization=org, current_user=user)
        c_total = c_ghost = c_new = 0
        for s in (ctrl_review["suggestions"] if ctrl_review else []):
            for h in s["hunks"]:
                c_total += 1
                for ln in (h.get("after") or "").splitlines():
                    if not ln.strip():
                        continue
                    if ln.strip() in ctrl_live_lines:
                        c_ghost += 1
                    else:
                        c_new += 1
                        print(f"  [control add] {ln.strip()[:95]}")

        print("\n" + "=" * 70)
        print(f"SCENARIO B (fresh base, same rewrite): {c_total} hunk(s); "
              f"{c_ghost} already-live line(s), {c_new} new")
        # Pass condition: a stale base must produce the same clean review a
        # fresh one does. Before the BuildContent.base_version_id fix, A showed
        # 2 already-live lines and B showed 0 — that gap WAS the bug.
        ok = ghost_lines == 0 and c_ghost == 0
        print(f"VERDICT: {'PASS' if ok else 'FAIL'} — stale-base review "
              f"{'matches' if ok else 'DIVERGES FROM'} fresh-base review")
        print("=" * 70)
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))

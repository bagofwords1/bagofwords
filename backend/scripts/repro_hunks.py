"""DB-tracing feedback loop for per-hunk tracked changes (immutable cherry-pick).

Seeds a throwaway sqlite DB, builds a complex multi-suggestion scenario, drives
the InstructionService cherry-pick API, and traces the DB (via raw sqlite3) after
every action while asserting the invariants in docs/sandbox-feedback-loop.md.

Run:  PYTHONPATH=. ./.venv/bin/python scripts/repro_hunks.py
"""
import os
import sys
import asyncio
import hashlib
import sqlite3
import subprocess
import uuid
from datetime import datetime

REPRO_DB = os.path.join(os.path.dirname(__file__), "..", "db", "repro_hunks.db")
REPRO_DB = os.path.abspath(REPRO_DB)
os.environ["BOW_DATABASE_URL"] = f"sqlite:///{REPRO_DB}"

FAILS = []


def check(cond, msg):
    status = "ok  " if cond else "FAIL"
    print(f"    [{status}] {msg}")
    if not cond:
        FAILS.append(msg)


def trace(label):
    """Raw-SQL snapshot of exactly what's persisted."""
    c = sqlite3.connect(REPRO_DB)
    c.row_factory = sqlite3.Row
    print(f"\n── DB TRACE: {label} ──────────────────────────────")
    builds = c.execute("""
        SELECT id, build_number, is_main, status, source, base_build_id, rejected_hunks
        FROM instruction_builds WHERE deleted_at IS NULL ORDER BY build_number
    """).fetchall()
    for b in builds:
        rej = b["rejected_hunks"] or ""
        print(f"  build #{b['build_number']} {b['id'][:8]} main={b['is_main']} {b['status']:<16} {b['source']:<5} base={(b['base_build_id'] or '-')[:8]} rejected={rej}")
        for cnt in c.execute("""
            SELECT bc.instruction_id i, iv.version_number vn, substr(iv.text,1,48) t
            FROM build_contents bc JOIN instruction_versions iv ON iv.id=bc.instruction_version_id
            WHERE bc.build_id=?""", (b["id"],)).fetchall():
            print(f"        · instr {cnt['i'][:8]} v{cnt['vn']}: {cnt['t']!r}")
    c.close()


def db_main_text(instruction_id):
    c = sqlite3.connect(REPRO_DB); c.row_factory = sqlite3.Row
    row = c.execute("""
        SELECT iv.text FROM build_contents bc
        JOIN instruction_builds b ON b.id=bc.build_id
        JOIN instruction_versions iv ON iv.id=bc.instruction_version_id
        WHERE b.is_main=1 AND b.deleted_at IS NULL AND bc.instruction_id=? LIMIT 1
    """, (instruction_id,)).fetchone()
    c.close()
    return row["text"] if row else None


def build_content_versions(build_id):
    """The version ids in a build's contents — to assert immutability."""
    c = sqlite3.connect(REPRO_DB); c.row_factory = sqlite3.Row
    rows = c.execute("SELECT instruction_id, instruction_version_id FROM build_contents WHERE build_id=? ORDER BY instruction_id", (build_id,)).fetchall()
    c.close()
    return {(r["instruction_id"], r["instruction_version_id"]) for r in rows}


def _hash(t):
    return hashlib.sha256((t or "").encode()).hexdigest()


async def main():
    # Fresh DB + migrations
    if os.path.exists(REPRO_DB):
        os.remove(REPRO_DB)
    env = {**os.environ}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, env=env,
                   cwd=os.path.join(os.path.dirname(__file__), ".."), stdout=subprocess.DEVNULL)

    import main as _app  # noqa: F401 — configure all ORM mappers + routes
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.instruction import Instruction
    from app.models.instruction_version import InstructionVersion
    from app.models.instruction_build import InstructionBuild
    from app.models.build_content import BuildContent
    from app.services.instruction_service import InstructionService

    svc = InstructionService()

    # Admin permissions for the actor (so accept promotes to main).
    async def _admin_perms(db, user, org):
        return {"manage_instructions", "full_admin_access"}
    svc._get_user_permissions = _admin_perms  # type: ignore

    async with async_session_maker() as db:
        org = Organization(id=str(uuid.uuid4()), name="Repro Org")
        db.add(org)
        user = User(id=str(uuid.uuid4()), name="Admin", email="admin@repro.test", hashed_password="x")
        db.add(user)
        await db.commit()

        async def mk_version(instr_id, n, text):
            v = InstructionVersion(id=str(uuid.uuid4()), instruction_id=instr_id, version_number=n,
                                   text=text, status="published", load_mode="always",
                                   content_hash=_hash(text), created_by_user_id=user.id)
            db.add(v); await db.commit(); return v

        async def mk_build(source, is_main, base_id, status):
            n = (await db.execute(_select_max_build_number(InstructionBuild, org.id))).scalar() or 0
            b = InstructionBuild(id=str(uuid.uuid4()), build_number=n + 1, status=status, source=source,
                                 is_main=is_main, organization_id=org.id, base_build_id=base_id,
                                 created_by_user_id=user.id, total_instructions=0, added_count=0,
                                 modified_count=0, removed_count=0)
            db.add(b); await db.commit(); return b

        async def add_content(build_id, instr_id, version_id):
            db.add(BuildContent(id=str(uuid.uuid4()), build_id=build_id, instruction_id=instr_id,
                                instruction_version_id=version_id))
            await db.commit()

        from sqlalchemy import func as _func, select as _select
        def _select_max_build_number(model, org_id):
            return _select(_func.max(model.build_number)).where(model.organization_id == org_id)

        # ── Seed: instruction SALES_OVERVIEW, main v0 ────────────────────────
        v0_text = ("Use Sales for analytics.\n\n"
                   "Key tables:\n- Track: tracks.\n- Album: albums.\n\n"
                   "Notes here.")
        instr = Instruction(id=str(uuid.uuid4()), text=v0_text, title="SALES_OVERVIEW",
                            status="published", category="general", kind="instruction",
                            organization_id=org.id, user_id=user.id, load_mode="always")
        db.add(instr); await db.commit()
        v0 = await mk_version(instr.id, 1, v0_text)
        instr.current_version_id = v0.id; await db.commit()
        main_build = await mk_build("user", True, None, "approved")
        await add_content(main_build.id, instr.id, v0.id)

        IID = instr.id
        trace("seed (main v0)")

        # ── Suggestion A (user): prepend HEADER + change "tracks"->"songs" ──
        a_text = ("HEADER.\nUse Sales for analytics.\n\n"
                  "Key tables:\n- Track: songs.\n- Album: albums.\n\nNotes here.")
        vA = await mk_version(IID, 100, a_text)
        bA = await mk_build("user", False, main_build.id, "pending_approval")
        await add_content(bA.id, IID, vA.id)

        # ── Suggestion B (ai): same prepend (overlap) + "albums"->"records" +
        #    append a trailing line. Disjoint from A except the shared prepend. ─
        b_text = ("HEADER.\nUse Sales for analytics.\n\n"
                  "Key tables:\n- Track: tracks.\n- Album: records.\n\nNotes here.\nMore notes.")
        vB = await mk_version(IID, 101, b_text)
        bB = await mk_build("ai", False, main_build.id, "pending_approval")
        await add_content(bB.id, IID, vB.id)

        trace("after creating suggestions A and B")
        before_A = build_content_versions(bA.id)
        before_B = build_content_versions(bB.id)

        # ── Read review hunks ────────────────────────────────────────────────
        r = await svc.review_hunks(db, IID, organization=org, current_user=user)
        sugg = {s["build_id"]: s for s in r["suggestions"]}
        print("\n  review: suggestions =", [(s["source"], len(s["hunks"])) for s in r["suggestions"]])
        check(bA.id in sugg and len(sugg[bA.id]["hunks"]) == 2, f"A has 2 hunks (prepend, tracks->songs)")
        check(bB.id in sugg and len(sugg[bB.id]["hunks"]) == 3, f"B has 3 hunks (prepend, albums->records, +trailing)")

        # ── Accept the word change in A (tracks -> songs) ───────────────────
        word_hunk = next(h for h in sugg[bA.id]["hunks"] if "songs" in h["after"])
        res, st = await svc.accept_hunk(db, IID, build_id=bA.id, hunk_key=word_hunk["key"],
                                        against_main_version_id=r["main_version_id"], organization=org, current_user=user)
        check(st == "ok", f"accept tracks->songs ok (got {st})")
        trace("after ACCEPT A:tracks->songs")
        check("Track: songs" in (db_main_text(IID) or ""), "main now says 'Track: songs'")
        check("HEADER" not in (db_main_text(IID) or ""), "main did NOT get the prepend (not accepted)")
        check(build_content_versions(bA.id) == before_A, "suggestion A build UNCHANGED (immutable)")
        check(build_content_versions(bB.id) == before_B, "suggestion B build UNCHANGED (immutable)")
        r2 = await svc.review_hunks(db, IID, organization=org, current_user=user)
        s2 = {s["build_id"]: s for s in r2["suggestions"]}
        check(len(s2[bA.id]["hunks"]) == 1, "A now has 1 hunk (word-change dropped out)")

        # ── Accept the shared prepend via B → A's prepend de-dupes away ─────
        prepend_B = next(h for h in s2[bB.id]["hunks"] if "HEADER" in h["after"])
        await svc.accept_hunk(db, IID, build_id=bB.id, hunk_key=prepend_B["key"],
                              against_main_version_id=r2["main_version_id"], organization=org, current_user=user)
        trace("after ACCEPT B:prepend (shared with A)")
        check((db_main_text(IID) or "").count("HEADER") == 1, "prepend appears exactly once (no duplicate)")
        r3 = await svc.review_hunks(db, IID, organization=org, current_user=user)
        s3 = {s["build_id"]: s.get("hunks", []) for s in r3["suggestions"]}
        check(bA.id not in s3, "suggestion A fully resolved (its only-left hunk, the prepend, de-duped via main)")

        # ── Reject B's albums->records; keep its trailing ──────────────────
        b_remaining = next(s for s in r3["suggestions"] if s["build_id"] == bB.id)["hunks"]
        album_hunk = next(h for h in b_remaining if "records" in h["after"])
        await svc.reject_hunk(db, IID, build_id=bB.id, hunk_key=album_hunk["key"], organization=org, current_user=user)
        trace("after REJECT B:albums->records")
        c = sqlite3.connect(REPRO_DB); c.row_factory = sqlite3.Row
        rej = c.execute("SELECT rejected_hunks FROM instruction_builds WHERE id=?", (bB.id,)).fetchone()["rejected_hunks"]
        c.close()
        check(album_hunk["key"] in (rej or ""), "B.rejected_hunks contains the albums->records key")
        r4 = await svc.review_hunks(db, IID, organization=org, current_user=user)
        s4 = {s["build_id"]: s["hunks"] for s in r4["suggestions"]}
        check(bB.id in s4 and len(s4[bB.id]) == 1, "B has 1 hunk left (reject one != reject all)")
        check(all("records" not in h["after"] for h in s4[bB.id]), "rejected albums->records gone")

        # ── Accept B's remaining trailing → B fully resolved ────────────────
        trailing = s4[bB.id][0]
        await svc.accept_hunk(db, IID, build_id=bB.id, hunk_key=trailing["key"],
                              against_main_version_id=r4["main_version_id"], organization=org, current_user=user)
        trace("after ACCEPT B:trailing")
        final = await svc.review_hunks(db, IID, organization=org, current_user=user)
        check(not final["suggestions"], "all suggestions resolved (none remain)")
        mt = db_main_text(IID) or ""
        check("More notes" in mt and "Album: albums" in mt and "records" not in mt,
              "final main = prepend + songs + trailing, albums kept (records rejected)")

        # ── Deletion test (isolated, no conflict): suggestion D removes a line.
        #    D forks from the CURRENT main, so its intent is a clean deletion.
        d_text = mt.replace("\nNotes here.", "")   # delete the "Notes here." line
        cur_main = (await db.execute(
            _select(InstructionBuild.id).where(InstructionBuild.organization_id == org.id, InstructionBuild.is_main == True)
        )).scalar()
        vD = await mk_version(IID, 200, d_text)
        bD = await mk_build("user", False, cur_main, "pending_approval")
        await add_content(bD.id, IID, vD.id)
        rd = await svc.review_hunks(db, IID, organization=org, current_user=user)
        d_hunks = next(s for s in rd["suggestions"] if s["build_id"] == bD.id)["hunks"]
        check(len(d_hunks) == 1 and d_hunks[0]["before"].strip().startswith("Notes"),
              "D shows 1 deletion hunk")
        await svc.accept_hunk(db, IID, build_id=bD.id, hunk_key=d_hunks[0]["key"],
                              against_main_version_id=rd["main_version_id"], organization=org, current_user=user)
        trace("after ACCEPT D:delete line")
        check("Notes here." not in (db_main_text(IID) or ""), "deleted line is gone from main")
        rd2 = await svc.review_hunks(db, IID, organization=org, current_user=user)
        check(not rd2["suggestions"], "D resolved; deletion NOT re-added (no re-add on accept)")

        # ── Reproducibility: every historical build_id still resolves ───────
        c = sqlite3.connect(REPRO_DB); c.row_factory = sqlite3.Row
        for bid in (bA.id, bB.id, bD.id, main_build.id):
            cnt = c.execute("SELECT COUNT(*) n FROM build_contents WHERE build_id=?", (bid,)).fetchone()["n"]
            check(cnt >= 1, f"historical build {bid[:8]} still has its immutable contents ({cnt} rows)")
        c.close()

    print("\n" + ("=" * 60))
    if FAILS:
        print(f"FAILED ({len(FAILS)}):")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("ALL INVARIANTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())

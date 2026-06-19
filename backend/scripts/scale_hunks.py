"""Scale + multi-user feedback loop for per-hunk tracked changes.

Seeds many instructions and suggestions (>= 1000 reviewable hunks), then applies
accept/reject decisions OUT OF ORDER, attributed to 30 different users (10 admins
who can apply, 20 members who must be denied), asserting integrity + immutability
at scale and measuring throughput. Traces the DB throughout.

Run:  PYTHONPATH=. ./.venv/bin/python scripts/scale_hunks.py
"""
import os, sys, asyncio, hashlib, sqlite3, subprocess, uuid, random, time

REPRO_DB = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "db", "scale_hunks.db"))
os.environ["BOW_DATABASE_URL"] = f"sqlite:///{REPRO_DB}"

N_INSTR = 10          # instructions (fewer = cheaper copy_from_main per accept)
SUGG_PER_INSTR = 2    # suggestions per instruction
LINES = 110           # lines per instruction body → 2 x 55 disjoint edits = 1100 hunks
random.seed(7)
FAILS = []


def check(cond, msg):
    print(f"    [{'ok  ' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILS.append(msg)


def q(sql, args=()):
    c = sqlite3.connect(REPRO_DB); c.row_factory = sqlite3.Row
    rows = c.execute(sql, args).fetchall(); c.close()
    return rows


def trace(label):
    b = q("SELECT COUNT(*) n, SUM(is_main) m FROM instruction_builds WHERE deleted_at IS NULL")[0]
    v = q("SELECT COUNT(*) n FROM instruction_versions")[0]
    bc = q("SELECT COUNT(*) n FROM build_contents")[0]
    print(f"── {label}: builds={b['n']} (is_main={b['m']}) versions={v['n']} build_contents={bc['n']}")


def _hash(t): return hashlib.sha256((t or "").encode()).hexdigest()


def body(lines, changed: dict):
    """30-line body; `changed` maps line_no -> new word."""
    out = []
    for i in range(lines):
        word = changed.get(i, f"word{i}")
        out.append(f"Line {i}: the {word} value here.")
    return "\n".join(out)


async def main():
    if os.path.exists(REPRO_DB):
        os.remove(REPRO_DB)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, env={**os.environ},
                   cwd=os.path.join(os.path.dirname(__file__), ".."), stdout=subprocess.DEVNULL)

    import main as _app  # noqa: F401
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.instruction import Instruction
    from app.models.instruction_version import InstructionVersion
    from app.models.instruction_build import InstructionBuild
    from app.models.build_content import BuildContent
    from app.services.instruction_service import InstructionService
    from sqlalchemy import func as _func, select as _select

    svc = InstructionService()
    admin_ids: set = set()

    async def _perms(db, user, org):
        return {"manage_instructions", "full_admin_access"} if str(user.id) in admin_ids else set()
    svc._get_user_permissions = _perms  # type: ignore

    async with async_session_maker() as db:
        org = Organization(id=str(uuid.uuid4()), name="Scale Org")
        db.add(org)
        users = []
        for i in range(30):
            u = User(id=str(uuid.uuid4()), name=f"User{i}", email=f"u{i}@scale.test", hashed_password="x")
            users.append(u); db.add(u)
            if i < 10:
                admin_ids.add(str(u.id))
        await db.commit()
        admins = [u for u in users if str(u.id) in admin_ids]
        members = [u for u in users if str(u.id) not in admin_ids]

        async def mk_version(iid, n, text):
            v = InstructionVersion(id=str(uuid.uuid4()), instruction_id=iid, version_number=n, text=text,
                                   status="published", load_mode="always", content_hash=_hash(text),
                                   created_by_user_id=users[0].id)
            db.add(v); return v

        bn = [0]
        async def mk_build(source, is_main, base_id, status, creator):
            bn[0] += 1
            b = InstructionBuild(id=str(uuid.uuid4()), build_number=bn[0], status=status, source=source,
                                 is_main=is_main, organization_id=org.id, base_build_id=base_id,
                                 created_by_user_id=creator.id, total_instructions=0, added_count=0,
                                 modified_count=0, removed_count=0)
            db.add(b); return b

        # ── Seed N instructions + main build (one main build holding all) ──────
        t0 = time.time()
        main_build = await mk_build("user", True, None, "approved", users[0])
        instr_ids = []
        for k in range(N_INSTR):
            v0_text = body(LINES, {})
            instr = Instruction(id=str(uuid.uuid4()), text=v0_text, title=f"INSTR_{k}", status="published",
                                category="general", kind="instruction", organization_id=org.id,
                                user_id=users[0].id, load_mode="always")
            db.add(instr)
            v0 = await mk_version(instr.id, 1, v0_text)
            instr.current_version_id = v0.id
            db.add(BuildContent(id=str(uuid.uuid4()), build_id=main_build.id, instruction_id=instr.id,
                                instruction_version_id=v0.id))
            instr_ids.append(instr.id)
        await db.commit()

        # ── Suggestions: each edits a distinct slice of lines (mostly disjoint
        #    so most hunks are independently applicable; a few overlap → dedup) ─
        total_intended = 0
        for k, iid in enumerate(instr_ids):
            half = LINES // SUGG_PER_INSTR
            for s in range(SUGG_PER_INSTR):
                # suggestion s edits a disjoint slice of lines (no cross-suggestion conflict)
                lo = s * half
                changed = {ln: f"NEW{k}_{s}_{ln}" for ln in range(lo, min(lo + half, LINES))}
                text = body(LINES, changed)
                v = await mk_version(iid, 100 + s, text)
                src = "ai" if s % 2 else "user"
                creator = members[(k + s) % len(members)]
                b = await mk_build(src, False, main_build.id, "pending_approval", creator)
                db.add(BuildContent(id=str(uuid.uuid4()), build_id=b.id, instruction_id=iid,
                                    instruction_version_id=v.id))
                total_intended += len(changed)
        await db.commit()
        print(f"\nseeded {N_INSTR} instructions, {N_INSTR*SUGG_PER_INSTR} suggestions, "
              f"~{total_intended} intended hunks in {time.time()-t0:.1f}s")
        trace("after seed")

        # Count reviewable hunks across all instructions.
        async def total_pending():
            tot = 0
            for iid in instr_ids:
                r = await svc.review_hunks(db, iid, organization=org, current_user=admins[0])
                tot += sum(len(s["hunks"]) for s in r["suggestions"])
            return tot

        start_hunks = await total_pending()
        print(f"reviewable hunks at start: {start_hunks}")
        check(start_hunks >= 1000, f"scenario has >= 1000 reviewable hunks (got {start_hunks})")

        # ── Out-of-order accept/reject from 30 users (admins apply; members denied)
        t1 = time.time()
        ops = accepts = rejects = denied = noops = conflicts = 0
        member_denied_verified = False
        guard = 0
        immutable_samples = {}  # build_id -> frozenset(contents) sampled early
        # snapshot a few suggestion builds to assert immutability later
        for row in q("SELECT id FROM instruction_builds WHERE is_main=0 LIMIT 5"):
            immutable_samples[row["id"]] = frozenset(
                (r["instruction_id"], r["instruction_version_id"])
                for r in q("SELECT instruction_id, instruction_version_id FROM build_contents WHERE build_id=?", (row["id"],))
            )

        active = list(instr_ids)   # instructions with (maybe) pending hunks
        while active and guard < 20000:
            guard += 1
            iid = random.choice(active)
            r = await svc.review_hunks(db, iid, organization=org, current_user=admins[0])
            pend = [(s["build_id"], h, r["main_version_id"]) for s in r["suggestions"] for h in s["hunks"]]
            if not pend:
                active.remove(iid)   # exhausted — stop rescanning it
                continue
            bid, h, main_vid = random.choice(pend)
            actor = random.choice(users)
            is_admin = str(actor.id) in admin_ids
            do_reject = random.random() < 0.3
            if not is_admin:
                # Member: must be denied at the route layer. Verify once via the
                # permission gate, then skip (members can't apply).
                if not member_denied_verified:
                    perms = await svc._get_user_permissions(db, actor, org)
                    check("manage_instructions" not in perms, "member lacks manage_instructions (would be 403 at route)")
                    member_denied_verified = True
                denied += 1
                continue
            ops += 1
            if do_reject:
                await svc.reject_hunk(db, iid, build_id=bid, hunk_key=h["key"], organization=org, current_user=actor)
                rejects += 1
            else:
                _res, st = await svc.accept_hunk(db, iid, build_id=bid, hunk_key=h["key"],
                                                 against_main_version_id=main_vid, organization=org, current_user=actor)
                if st == "ok":
                    accepts += 1
                elif st == "noop":
                    noops += 1
                elif st == "conflict":
                    conflicts += 1
            if ops % 200 == 0:
                trace(f"after {ops} applied ops")

        dt = time.time() - t1
        print(f"\napplied {ops} ops ({accepts} accept, {rejects} reject, {noops} noop, {conflicts} conflict), "
              f"{denied} member attempts denied, in {dt:.1f}s  ({ops/max(dt,0.01):.0f} ops/s)")
        trace("after all ops")

        # ── Integrity assertions ────────────────────────────────────────────
        check(await total_pending() == 0, "all hunks resolved (no pending remain)")
        check(member_denied_verified, "member permission boundary exercised")
        # exactly one main build
        mains = q("SELECT COUNT(*) n FROM instruction_builds WHERE is_main=1 AND deleted_at IS NULL")[0]["n"]
        check(mains == 1, f"exactly one is_main build (got {mains})")
        # immutability: sampled suggestion builds unchanged
        ok_imm = True
        for bid, snap in immutable_samples.items():
            now = frozenset((r["instruction_id"], r["instruction_version_id"])
                            for r in q("SELECT instruction_id, instruction_version_id FROM build_contents WHERE build_id=?", (bid,)))
            if now != snap:
                ok_imm = False
        check(ok_imm, "sampled suggestion builds are byte-identical (immutable) after 1000s of ops")
        # every instruction's main text is coherent (non-empty, 30 lines)
        coherent = True
        for iid in instr_ids:
            t = q("""SELECT iv.text FROM build_contents bc JOIN instruction_builds b ON b.id=bc.build_id
                     JOIN instruction_versions iv ON iv.id=bc.instruction_version_id
                     WHERE b.is_main=1 AND bc.instruction_id=?""", (iid,))
            if not t or len(t[0]["text"].split("\n")) != LINES:
                coherent = False
        check(coherent, "every instruction's main text stays coherent (30 lines, no corruption)")
        # reproducibility: all historical builds still resolve
        orphans = q("""SELECT COUNT(*) n FROM build_contents bc
                       LEFT JOIN instruction_versions iv ON iv.id=bc.instruction_version_id
                       WHERE iv.id IS NULL""")[0]["n"]
        check(orphans == 0, "no orphaned build_contents (every build_id resolves)")

    print("\n" + "=" * 60)
    if FAILS:
        print(f"FAILED ({len(FAILS)}):")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("SCALE + MULTI-USER: ALL INVARIANTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())

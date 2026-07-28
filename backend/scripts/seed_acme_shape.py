"""Seed the shape that makes the /agents page slow in a long-lived workspace.

Modelled on a real deployment's measured profile:

    139 instructions · 320 builds (169 open draft/pending) · 99 pending ids
    180 candidate changed rows · 76 needing expensive reconciliation
    text sizes: median 3,568 · mean 4,382 · p90 8,235 · max 15,242 chars
    up to 35 suggestion builds on a single instruction

What makes a row *expensive* is not its existence — it is that **main moved
after the suggestion forked**. When `base == main`, the pending check answers
from cheap equality short-circuits. When main has drifted, the check has to run
the word-level 3-way rebase (`rebased_hunks_against_main`), which is quadratic
in the text length. So the seeder deliberately promotes a NEW main build after
the drafts fork, which is exactly how a real workspace gets there: people keep
publishing while older suggestions sit unreviewed.

Presets:
  acme    — the deployment above, 1x
  acme50  — 50x the reconciliation work (the cost driver), same text profile

Usage:
  python scripts/seed_acme_shape.py acme
  python scripts/seed_acme_shape.py acme50
  python scripts/seed_acme_shape.py custom --instructions 500 --open-builds 300 \
      --expensive 400 --max-suggestions 35
"""
import os
import sys
import uuid
import random
import asyncio
import hashlib
import argparse

os.environ.setdefault("BOW_DATABASE_URL", "sqlite:///db/app.db")
os.environ.setdefault("BOW_SMTP_PASSWORD", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select, insert, text as sqltext

import app.models  # noqa
import pkgutil, importlib
for _, modname, _ in pkgutil.iter_modules(app.models.__path__):
    if modname == "application":
        continue
    importlib.import_module(f"app.models.{modname}")

from app.models.user import User
from app.models.organization import Organization
from app.models.data_source import DataSource
from app.models.data_source_membership import DataSourceMembership
from app.models.instruction import Instruction, instruction_data_source_association
from app.models.instruction_version import InstructionVersion
from app.models.instruction_build import InstructionBuild
from app.models.build_content import BuildContent

PRESETS = {
    # instructions, open draft builds, rows needing expensive reconciliation,
    # max suggestion builds on one instruction, agents
    # not_in_main = instructions that exist only inside a draft build (someone
    # proposed a NEW instruction that was never published). They are what makes
    # the "All instructions" badge over-report: they are folded into the live
    # surfaces as pending AND counted again as not-live.
    "acme":   dict(instructions=139,  open_builds=169, expensive=76,   max_suggestions=35, agents=3,
                   not_in_main=81),
    "acme50": dict(instructions=1000, open_builds=800, expensive=3800, max_suggestions=35, agents=8,
                   not_in_main=580),
}

# Word pool + size distribution reproducing the measured character profile.
_WORDS = (
    "revenue orders refunded customer segment exclude include join filter active account date report "
    "window trailing months cancelled partial currency workspace rule booking occupancy reservation "
    "channel margin discount tax net gross region property nights adults children rate plan cancellation "
    "policy lead source arrival departure status confirmed denominator numerator threshold rolling"
).split()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _text(rng: random.Random, nchars: int) -> str:
    out, n = [], 0
    while n < nchars:
        w = rng.choice(_WORDS)
        out.append(w)
        n += len(w) + 1
    return " ".join(out)


def _size(rng: random.Random) -> int:
    """Draw a length matching median 3,568 / mean 4,382 / p90 8,235 / max 15,242."""
    r = rng.random()
    if r < 0.50:
        return rng.randint(1200, 3568)
    if r < 0.90:
        return rng.randint(3568, 8235)
    return rng.randint(8235, 15242)


def _edit(rng: random.Random, s: str, nwords: int = 8) -> str:
    """A localized edit — one region rewritten, the rest untouched. This is what
    a real suggestion looks like, and it is the case the diff is worst at:
    the texts stay long and almost identical."""
    toks = s.split()
    if len(toks) <= nwords:
        return s + " " + " ".join(rng.choice(_WORDS) for _ in range(nwords))
    i = rng.randrange(0, len(toks) - nwords)
    return " ".join(toks[:i] + [rng.choice(_WORDS) for _ in range(nwords)] + toks[i + nwords:])


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("preset", nargs="?", default="acme", choices=[*PRESETS, "custom"])
    ap.add_argument("--instructions", type=int)
    ap.add_argument("--open-builds", type=int)
    ap.add_argument("--expensive", type=int)
    ap.add_argument("--max-suggestions", type=int)
    ap.add_argument("--agents", type=int)
    ap.add_argument("--not-in-main", type=int)
    args = ap.parse_args()

    cfg = dict(PRESETS.get(args.preset, PRESETS["acme"]))
    for k in ("instructions", "open_builds", "expensive", "max_suggestions", "agents", "not_in_main"):
        v = getattr(args, k if k != "open_builds" else "open_builds", None)
        if v:
            cfg[k] = v
    n_instr, n_builds = cfg["instructions"], cfg["open_builds"]
    n_expensive, max_sug, n_agents = cfg["expensive"], cfg["max_suggestions"], cfg["agents"]
    n_not_in_main = cfg["not_in_main"]
    print(f"preset={args.preset} {cfg}")

    rng = random.Random(4242)
    url = os.environ["BOW_DATABASE_URL"]
    url = (url.replace("postgresql://", "postgresql+asyncpg://", 1) if url.startswith("postgresql://")
           else url.replace("sqlite:///", "sqlite+aiosqlite:///", 1) if url.startswith("sqlite:///") else url)
    engine = create_async_engine(url, future=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        user = (await db.execute(select(User).where(User.email == "sandbox@bow.dev"))).scalars().first()
        org = (await db.execute(select(Organization))).scalars().first()
        assert user and org, "register sandbox@bow.dev first"
        org_id, uid = str(org.id), str(user.id)

        # ── Agents ──────────────────────────────────────────────────────────
        agents = []
        for a in range(n_agents):
            ds = DataSource(id=str(uuid.uuid4()), name=f"Agent-V{a+1}", is_active=True,
                            organization_id=org_id)
            db.add(ds)
            await db.flush()
            db.add(DataSourceMembership(id=str(uuid.uuid4()), data_source_id=str(ds.id),
                                        principal_type="user", principal_id=uid))
            agents.append(str(ds.id))
        await db.flush()

        next_bn = ((await db.execute(
            select(InstructionBuild.build_number).where(InstructionBuild.organization_id == org_id)
            .order_by(InstructionBuild.build_number.desc()).limit(1)
        )).scalar() or 0) + 1

        # ── Build B0: every instruction at v1 ───────────────────────────────
        b0 = InstructionBuild(id=str(uuid.uuid4()), build_number=next_bn, status="approved",
                              source="user", is_main=False, organization_id=org_id,
                              created_by_user_id=uid)
        next_bn += 1
        db.add(b0)
        await db.flush()
        b0_id = str(b0.id)

        instr_ids, v1_ids, v1_texts = [], [], []
        rows = []
        for i in range(n_instr):
            iid = str(uuid.uuid4())
            body = _text(rng, _size(rng))
            db.add(Instruction(id=iid, text=body, title=f"Rule {i}", status="published",
                               category="general", kind="instruction", user_id=uid,
                               organization_id=org_id, source_type="user", load_mode="always",
                               thumbs_up=0, is_seen=True, can_user_toggle=True))
            vid = str(uuid.uuid4())
            db.add(InstructionVersion(id=vid, instruction_id=iid, version_number=1, text=body,
                                      title=f"Rule {i}", status="published", load_mode="always",
                                      content_hash=_hash(body), created_by_user_id=uid))
            rows.append({"instruction_id": iid, "data_source_id": agents[i % len(agents)]})
            instr_ids.append(iid); v1_ids.append(vid); v1_texts.append(body)
            if (i + 1) % 200 == 0:
                await db.flush()
                print(f"  instructions {i+1}/{n_instr}")
        await db.flush()
        await db.execute(instruction_data_source_association.insert(), rows)
        # The tail is deliberately absent from the build: those instructions live
        # only inside draft builds (unpublished proposals).
        live_idx = list(range(n_instr - n_not_in_main))
        await db.execute(insert(BuildContent.__table__), [
            {"id": str(uuid.uuid4()), "build_id": b0_id, "instruction_id": instr_ids[i],
             "instruction_version_id": v1_ids[i], "is_change": True}
            for i in live_idx
        ])
        await db.commit()
        print(f"  base build B0 with {n_instr} instructions")

        # ── Open draft builds, forked from B0 ───────────────────────────────
        # Each snapshots everything (carry-over) and changes a few instructions.
        # `hot` gets max_suggestions of them, mirroring "35 suggestions on one".
        hot = 0
        changed_by_build = []
        for b in range(n_builds):
            if b < max_sug:
                changed = [hot]
            else:
                k = rng.randint(1, 3)
                changed = [rng.randrange(n_instr - n_not_in_main) for _ in range(k)]
            changed_by_build.append(sorted(set(changed)))
        # Spread the unpublished proposals across the draft builds.
        for j, idx in enumerate(range(n_instr - n_not_in_main, n_instr)):
            changed_by_build[j % n_builds] = sorted(set(changed_by_build[j % n_builds] + [idx]))

        for b, changed in enumerate(changed_by_build):
            sug = InstructionBuild(id=str(uuid.uuid4()), build_number=next_bn, status="draft",
                                   source="ai", is_main=False, organization_id=org_id,
                                   base_build_id=b0_id, created_by_user_id=uid,
                                   description=f"Suggestion {b}")
            next_bn += 1
            db.add(sug)
            await db.flush()
            sug_id = str(sug.id)

            proposed_vid = {}
            for idx in changed:
                ptext = _edit(rng, v1_texts[idx])
                pv = str(uuid.uuid4())
                db.add(InstructionVersion(id=pv, instruction_id=instr_ids[idx],
                                          version_number=2 + b, text=ptext, title=f"Rule {idx}",
                                          status="published", load_mode="always",
                                          content_hash=_hash(ptext), created_by_user_id=uid))
                proposed_vid[idx] = pv
            await db.flush()

            carried = set(live_idx) | set(proposed_vid)
            await db.execute(insert(BuildContent.__table__), [
                {"id": str(uuid.uuid4()), "build_id": sug_id, "instruction_id": instr_ids[i],
                 "instruction_version_id": proposed_vid.get(i, v1_ids[i]),
                 "is_change": i in proposed_vid}
                for i in sorted(carried)
            ])
            if (b + 1) % 50 == 0:
                await db.commit()
                print(f"  draft builds {b+1}/{n_builds}")
        await db.commit()

        # ── Move main forward, AFTER the drafts forked ──────────────────────
        # This is what makes reconciliation expensive: for these instructions
        # the drafts' base (B0) no longer matches main, so the pending check
        # must rebase the suggestion's intent onto the drifted main text.
        drifted = list(range(min(n_expensive, n_instr - n_not_in_main)))
        main_versions = dict(zip(range(n_instr), v1_ids))
        for idx in drifted:
            mtext = _edit(rng, v1_texts[idx], nwords=12)
            mv = str(uuid.uuid4())
            db.add(InstructionVersion(id=mv, instruction_id=instr_ids[idx], version_number=1000 + idx,
                                      text=mtext, title=f"Rule {idx}", status="published",
                                      load_mode="always", content_hash=_hash(mtext),
                                      created_by_user_id=uid))
            main_versions[idx] = mv
        await db.flush()

        b1 = InstructionBuild(id=str(uuid.uuid4()), build_number=next_bn, status="approved",
                              source="user", is_main=True, organization_id=org_id,
                              base_build_id=b0_id, created_by_user_id=uid)
        db.add(b1)
        await db.flush()
        await db.execute(insert(BuildContent.__table__), [
            {"id": str(uuid.uuid4()), "build_id": str(b1.id), "instruction_id": instr_ids[i],
             "instruction_version_id": main_versions[i], "is_change": i in set(drifted)}
            for i in live_idx
        ])
        await db.execute(sqltext(
            "UPDATE instructions SET current_version_id = :v WHERE id = :i"
        ), [{"v": main_versions[i], "i": instr_ids[i]} for i in range(n_instr)])
        await db.commit()

        total_bc = (await db.execute(sqltext("SELECT COUNT(*) FROM build_contents"))).scalar()
        print(f"\nseeded: {n_instr} instructions ({n_not_in_main} never published) · "
              f"{n_builds} open draft builds · {len(drifted)} drifted (expensive) · "
              f"{total_bc:,} build_content rows")
        print(f"  main build      = {b1.id}")
        print(f"  hot instruction = {instr_ids[hot]} ({max_sug} suggestions)")
        print(f"  agents          = {agents}")


if __name__ == "__main__":
    asyncio.run(main())

"""Benchmark + equivalence capture for the instruction 'pending changes' sweep
(instruction_service.get_pending_change_instruction_ids) and the unfiltered
instruction list — the org-wide path that materialized every pending build's
contents. Prints wall time and an md5 of the resulting id set so before/after
runs can be diffed for behavioural equivalence.

Usage (from backend/, running-stack env):
  uv run python scripts/bench_pending_sweep.py <org_id> <admin_email> <password>
"""
import asyncio
import hashlib
import sys
import time

import main  # noqa: F401

from sqlalchemy import select
from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.services.instruction_service import InstructionService

ORG_ID = sys.argv[1]
EMAIL = sys.argv[2]


async def run():
    svc = InstructionService()
    async with async_session_maker() as db:
        org = (await db.execute(select(Organization).where(Organization.id == ORG_ID))).scalar_one()
        user = (await db.execute(select(User).where(User.email == EMAIL))).scalar_one()

        # warm
        ids = await svc.get_pending_change_instruction_ids(db, org, user)
        # timed
        t0 = time.perf_counter()
        ids = await svc.get_pending_change_instruction_ids(db, org, user)
        sweep_ms = (time.perf_counter() - t0) * 1000
        digest = hashlib.md5("\n".join(sorted(str(i) for i in ids)).encode()).hexdigest()[:12]
        print(f"SWEEP get_pending_change_instruction_ids: {sweep_ms:.0f} ms, {len(ids)} ids, md5={digest}")

        # full unfiltered list (what home/agents prefetch: limit=50)
        t0 = time.perf_counter()
        res = await svc.get_instructions(db, org, user, skip=0, limit=50)
        list_ms = (time.perf_counter() - t0) * 1000
        total = res["total"]
        rows = res["items"]
        row_ids = hashlib.md5("\n".join(str(r.id) for r in rows).encode()).hexdigest()[:12]
        print(f"LIST GET /instructions?limit=50: {list_ms:.0f} ms, total={total}, page_rows={len(rows)}, page_md5={row_ids}")


if __name__ == "__main__":
    asyncio.run(run())

"""Micro-benchmark for the agent's instruction-context load (the completion
hot path: InstructionContextBuilder.build -> _load_from_build).

Measures wall time + peak rows hydrated for a report scoped to a heavy agent,
and prints the resulting instruction id set so before/after runs can be diffed
for behavioral equivalence. Also runs N concurrent builds to expose event-loop
serialization.

Usage (from backend/, with the running stack env):
  TESTING=true TEST_DATABASE_URL=... BOW_DATABASE_URL=... BOW_ENCRYPTION_KEY=... \
    uv run python scripts/bench_instruction_context.py <org_id> <agent_id> [user_id]
"""
import asyncio
import sys
import time

import main  # noqa: F401  register all models

from sqlalchemy import select, event
from sqlalchemy.engine import Engine

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

ORG_ID = sys.argv[1]
AGENT_ID = sys.argv[2]
USER_ID = sys.argv[3] if len(sys.argv) > 3 else None

_stmt_count = 0
_rows = 0


def _hook(conn, cursor, statement, params, context, executemany):
    global _stmt_count
    _stmt_count += 1


async def load_org_settings(db, org):
    try:
        from app.services.organization_settings_service import OrganizationSettingsService
        return await OrganizationSettingsService().get_settings(db, str(org.id))
    except Exception:
        return None


async def one_build():
    async with async_session_maker() as db:
        org = (await db.execute(select(Organization).where(Organization.id == ORG_ID))).scalar_one()
        user = None
        if USER_ID:
            user = (await db.execute(select(User).where(User.id == USER_ID))).scalar_one_or_none()
        settings = await load_org_settings(db, org)
        builder = InstructionContextBuilder(
            db, org, current_user=user, organization_settings=settings,
            data_source_ids=[AGENT_ID], mode="chat", channel="app",
        )
        section = await builder.build(query="Show total revenue by billing country", data_source_ids=[AGENT_ID])
        ids = sorted({it.id for it in (section.items or [])})
        return ids


async def main_run():
    global _stmt_count
    event.listen(Engine, "before_cursor_execute", _hook)

    # warm + correctness snapshot
    ids = await one_build()

    # timed single build
    _stmt_count = 0
    t0 = time.perf_counter()
    ids2 = await one_build()
    single_ms = (time.perf_counter() - t0) * 1000
    single_stmts = _stmt_count

    # concurrent builds (event-loop serialization proxy)
    _stmt_count = 0
    N = 8
    t0 = time.perf_counter()
    await asyncio.gather(*(one_build() for _ in range(N)))
    conc_ms = (time.perf_counter() - t0) * 1000

    event.remove(Engine, "before_cursor_execute", _hook)

    import hashlib
    digest = hashlib.md5("\n".join(ids).encode()).hexdigest()[:12]
    print(f"RESULT items={len(ids)} ids_md5={digest}")
    print(f"SINGLE build: {single_ms:.0f} ms, {single_stmts} SQL statements")
    print(f"CONCURRENT x{N}: {conc_ms:.0f} ms total ({conc_ms/N:.0f} ms/build)")


if __name__ == "__main__":
    asyncio.run(main_run())

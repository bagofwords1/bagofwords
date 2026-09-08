"""Backfill the diagnosis rollup columns on agent_executions.

    cd backend
    BOW_DATABASE_URL=... uv run python scripts/backfill_agent_execution_rollups.py [--all] [--org ORG_ID] [--batch 500]

Resumable: runs are stamped ``rollup_at`` as they are done, so a re-run
without ``--all`` only touches runs that have no rollup yet. Cost for runs
that predate usage-record attribution is recovered by report + time window
and flagged ``cost_is_partial``; runs before the attribution columns existed
show cost as unknown, never zero.
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def _main(args) -> None:
    import main  # noqa: F401 — registers every SQLAlchemy mapper
    from app.dependencies import async_session_maker
    from app.services.diagnosis.rollup import backfill

    started = time.monotonic()
    last = {"t": started}

    def progress(done: int, total: int) -> None:
        now = time.monotonic()
        if now - last["t"] >= 2 or done == total:
            rate = done / max(now - started, 1e-6)
            print(f"  {done}/{total} runs ({rate:.0f}/s)", flush=True)
            last["t"] = now

    async with async_session_maker() as db:
        done = await backfill(
            db,
            only_missing=not args.all,
            batch_size=args.batch,
            organization_id=args.org,
            progress=progress,
        )
    print(f"rolled up {done} runs in {time.monotonic() - started:.1f}s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="recompute every run, not only those without a rollup")
    parser.add_argument("--org", default=None, help="limit to one organization id")
    parser.add_argument("--batch", type=int, default=500, help="runs per commit")
    asyncio.run(_main(parser.parse_args()))

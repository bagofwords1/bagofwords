#!/usr/bin/env python3
"""Run one overnight dream NOW (debug path for the live feedback loop).

Runs the real runtime path — settings and per-target switches, the budget,
the run log, the dream itself on the org's small model, watermarks — in this
process against the same database, ignoring only the 01:00-05:00 night window
(and, with --force, the once-per-night dedupe).

    cd backend && TEST_DATABASE_URL=sqlite:///db/agent.db TESTING=true \\
        uv run python ../tools/agent/run_dream.py --kind agent --target <data_source_id> [--org <id>] [--force]
    cd backend && ... uv run python ../tools/agent/run_dream.py --kind user --target <user_id> [--org <id>] [--force]
    cd backend && ... uv run python ../tools/agent/run_dream.py --sweep   # one sweep tick, window honoured

Prints the dream_runs row as JSON.
"""
import argparse
import asyncio
import json
import sys


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=("agent", "user"))
    ap.add_argument("--target", help="data_source_id (agent) or user_id (user)")
    ap.add_argument("--org", help="organization id (defaults to the target's org)")
    ap.add_argument("--force", action="store_true", help="also bypass the once-per-night dedupe")
    ap.add_argument("--sweep", action="store_true", help="run one sweep tick instead (window honoured)")
    args = ap.parse_args()

    import main as _app  # noqa: F401  registers every mapper
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.services.dreams.runtime import dream_runtime

    if args.sweep:
        print(json.dumps(await dream_runtime.sweep(), default=str, indent=2))
        return 0
    if not args.kind or not args.target:
        ap.error("--kind and --target are required (or use --sweep)")

    org_id = args.org
    if not org_id:
        async with async_session_maker() as db:
            if args.kind == "agent":
                from app.models.data_source import DataSource
                ds = await db.get(DataSource, args.target)
                org_id = str(ds.organization_id) if ds else None
            else:
                from app.models.membership import Membership
                m = (await db.execute(select(Membership).where(Membership.user_id == args.target))).scalars().first()
                org_id = str(m.organization_id) if m else None
    if not org_id:
        print("could not resolve the organization; pass --org", file=sys.stderr)
        return 1

    run = await dream_runtime.run_unit(args.kind, org_id, args.target, ignore_window=True, force=args.force)
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.wait(pending, timeout=15)
    if run is None:
        print("not run: already ran tonight (use --force) or claimed elsewhere", file=sys.stderr)
        return 2
    print(json.dumps({
        "id": run.id, "kind": run.kind, "status": run.status, "status_reason": run.status_reason,
        "local_date": run.local_date, "tokens": run.tokens, "cost_usd": run.cost_usd,
        "inputs_summary": run.inputs_summary, "tool_calls": run.tool_calls, "outputs": run.outputs,
    }, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Backfill the `preview` field into persisted read-tool projections.

Report read endpoints serve `tool_execution.context_summary_json`, not
`result_json` (see services/report_payload_projection.py). Projections written
before `preview` joined the allowlist therefore render as plain text forever,
even though `result_json` holds a perfectly good render contract — and nothing
self-heals them: the ORM hook only fills a MISSING projection, and the read path
sets the rebuilt value in memory without writing it back.

Scoped deliberately to read_file / read_email / read_note. Bumping
CONTEXT_SUMMARY_VERSION would invalidate create_data and write_csv too, whose
projections exist precisely to avoid loading multi-megabyte row payloads on
every report open — measured at ~2.8 ms per MB, repaid on every open because
the rebuild is never persisted. These rows are small and capped (text is
truncated at UI_FILE_PREVIEW_CHARS), so rewriting them once is cheap.

Idempotent: rows whose projection already carries `preview` are skipped, so a
re-run is a no-op.

    python scripts/backfill_read_file_preview_projection.py --dry-run
    python scripts/backfill_read_file_preview_projection.py
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: F401  - registers every ORM mapper
from sqlalchemy import select

from app.ai.persisted_summary import SUMMARIZED_TOOL_NAMES, build_tool_context_summary
from app.dependencies import async_session_maker
from app.models.tool_execution import ToolExecution

READ_TOOLS = tuple(
    name for name in ("read_file", "read_email", "read_note") if name in SUMMARIZED_TOOL_NAMES
)
BATCH = 200


async def backfill(dry_run: bool) -> int:
    updated = skipped = unfixable = 0

    async with async_session_maker() as db:
        rows = (
            await db.execute(
                select(ToolExecution).where(ToolExecution.tool_name.in_(READ_TOOLS))
            )
        ).scalars().all()

        for i, execution in enumerate(rows, start=1):
            result = execution.result_json
            summary = execution.context_summary_json

            if not isinstance(result, dict) or "preview" not in result:
                # Predates the feature entirely — there is no contract to carry
                # forward. Re-reading the file is the only cure, and that is a
                # user action, not a migration.
                unfixable += 1
                continue
            if isinstance(summary, dict) and "preview" in summary:
                skipped += 1
                continue

            rebuilt = build_tool_context_summary(execution.tool_name, result)
            if not isinstance(rebuilt, dict) or "preview" not in rebuilt:
                unfixable += 1
                continue

            if not dry_run:
                execution.context_summary_json = rebuilt
            updated += 1

            if not dry_run and i % BATCH == 0:
                await db.commit()

        if not dry_run:
            await db.commit()

    verb = "would update" if dry_run else "updated"
    print(f"{verb}: {updated}")
    print(f"already had preview (skipped): {skipped}")
    print(f"no preview in result_json (needs a fresh read): {unfixable}")
    return updated


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args()
    asyncio.run(backfill(args.dry_run))


if __name__ == "__main__":
    cli()

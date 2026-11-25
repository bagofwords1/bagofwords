#!/usr/bin/env python3
"""Print the schema context the agent is given for one cached (custom) table:
the `<table>` element exactly as the planner's context renders it, built by
the application's own SchemaContextBuilder against the running app's database.

Used by docs/feedback-loops/cached-table-column-metadata.md to show the
`aggregation="not additive ..."` note on a column.

    cd backend && TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=sqlite:///db/agent.db \\
        uv run python ../tools/agent/cached_table_metadata_context.py <agent name> <table name>
"""
import asyncio
import re
import sys

from sqlalchemy import select
from sqlalchemy.orm import selectinload


async def main(agent: str, table: str) -> None:
    import main as _app  # noqa: F401 - registers every ORM model
    from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
    from app.dependencies import async_session_maker
    from app.models.data_source import DataSource
    from app.models.organization import Organization

    async with async_session_maker() as db:
        ds = (await db.execute(
            select(DataSource).where(DataSource.name == agent).options(selectinload(DataSource.connections))
        )).scalars().first()
        if ds is None:
            sys.exit(f"no agent named {agent!r}")
        org = await db.get(Organization, ds.organization_id)
        context = await SchemaContextBuilder(db, [ds], org, None).build()
        xml = context.render_combined(top_k_per_ds=20, index_limit=200)
    match = re.search(rf'<table name="{re.escape(table)}".*?</table>', xml, re.DOTALL)
    print(match.group(0) if match else f"table {table!r} is not in the rendered context:\n{xml[:2000]}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2]))

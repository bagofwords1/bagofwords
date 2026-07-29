"""Execute a real DAX query as a given member through the product's client stack.

Proves the end-to-end query path for an identity whose access to the model is
item-level (no workspace role) - the topology the workspace-scoped
executeQueries endpoint rejects.
"""
import asyncio
import os
import sys

_BACKEND = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "backend")
sys.path.insert(0, _BACKEND)

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession  # noqa: E402

DB_PATH = os.path.join(_BACKEND, "db", "app.db")
LOCAL_EMAIL = os.environ.get("BOW_RLS_LOCAL_EMAIL", "demo2@example.com")
TABLE = os.environ.get("BOW_RLS_TABLE", "shared_orders/Orders")
DAX = os.environ.get("BOW_RLS_DAX", "EVALUATE Orders")
# In an org with many agents "first data source" is a lottery — name the one
# to exercise. Unset keeps the old single-agent behavior.
DS_NAME = os.environ.get("BOW_RLS_DS")


async def main():
    import main as _appmain  # noqa: F401
    from app.models.user import User
    from app.models.data_source import DataSource
    from app.services.data_source_service import DataSourceService
    from sqlalchemy.orm import selectinload

    engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}")
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as db:
        user = (await db.execute(select(User).where(User.email == LOCAL_EMAIL))).scalar_one()
        ds_stmt = select(DataSource).options(selectinload(DataSource.connections))
        if DS_NAME:
            ds_stmt = ds_stmt.where(DataSource.name == DS_NAME)
        ds = (await db.execute(ds_stmt)).scalars().first()

        clients = await DataSourceService().construct_clients(db=db, data_source=ds, current_user=user)
        client = clients[ds.name]
        print(f"identity={LOCAL_EMAIL}  table={TABLE}")
        try:
            df = client.execute_query(DAX, TABLE)
            print(f"QUERY OK  rows={len(df)}  cols={list(df.columns)}")
            print(df.to_string(index=False)[:900])
            scoped = getattr(client, "_tenant_scoped_workspaces", set())
            print(f"tenant-level fallback used for workspace(s): {scoped or 'none (group-scoped worked)'}")
        except Exception as e:
            print(f"QUERY FAILED  {type(e).__name__}: {str(e)[:400]}")
    await engine.dispose()


asyncio.run(main())

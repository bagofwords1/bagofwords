"""What does each member actually SEE for a data source, at both layers:

  overlay  — the per-user catalog (what their token proved they can reach)
  context  — what the agent is allowed to put in front of the model

The two differ: a table must be in the user's overlay AND active on the shared
catalog to reach the agent. Models only a user can see are contributed inactive,
so this is the check that tells you whether admin activation propagates.
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
EMAILS = (os.environ.get("BOW_VIS_EMAILS")
          or "admin@example.com,demo1@example.com,demo2@example.com").split(",")
# In an org with many agents "first data source" is a lottery — name the one
# to inspect. Unset keeps the old single-agent behavior.
DS_NAME = os.environ.get("BOW_VIS_DS")


async def main():
    import main as _appmain  # noqa: F401
    from app.models.user import User
    from app.models.data_source import DataSource
    from app.models.organization import Organization
    from app.models.user_data_source_overlay import UserDataSourceTable
    from app.services.data_source_service import DataSourceService
    from sqlalchemy.orm import selectinload

    engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}")
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as db:
        ds_stmt = select(DataSource).options(selectinload(DataSource.connections))
        if DS_NAME:
            ds_stmt = ds_stmt.where(DataSource.name == DS_NAME)
        ds = (await db.execute(ds_stmt)).scalars().first()
        org = (await db.execute(select(Organization))).scalars().first()
        svc = DataSourceService()

        for email in EMAILS:
            email = email.strip()
            user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
            if not user:
                continue
            overlay = (await db.execute(select(UserDataSourceTable).where(
                UserDataSourceTable.data_source_id == str(ds.id),
                UserDataSourceTable.user_id == str(user.id),
                UserDataSourceTable.is_accessible == True,  # noqa: E712
            ))).scalars().all()
            ctx = await svc.get_data_source_schema(
                db=db, data_source_id=str(ds.id), organization=org,
                include_inactive=False, current_user=user)
            print(f"\n{email}")
            print(f"  overlay ({len(overlay)}): {sorted(o.table_name for o in overlay)}")
            print(f"  context ({len(ctx or [])}): {sorted(getattr(t, 'name', '?') for t in (ctx or []))}")
    await engine.dispose()


asyncio.run(main())

"""Profile ReportService.get_reports (filter=my, limit=50): wall time + SQL
statement count. Proves the lazy="selectin" cascade structurally.

Reads BOW_DATABASE_URL from env.
"""
import os, time, asyncio
os.environ.setdefault("BOW_SMTP_PASSWORD", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select
import app.models, pkgutil, importlib
for _, m, _ in pkgutil.iter_modules(app.models.__path__):
    if m != "application":
        importlib.import_module(f"app.models.{m}")
from app.models.user import User
from app.models.organization import Organization
from app.services.report_service import ReportService


def _url():
    u = os.environ["BOW_DATABASE_URL"]
    if u.startswith("postgresql://"): return u.replace("postgresql://", "postgresql+asyncpg://", 1)
    if u.startswith("sqlite:///"): return u.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return u


async def main():
    engine = create_async_engine(_url(), future=True)
    S = async_sessionmaker(engine, expire_on_commit=False)
    counter = {"n": 0}
    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _c(conn, cur, stmt, params, ctx, many): counter["n"] += 1
    async with S() as db:
        user = (await db.execute(select(User).where(User.email == "sandbox@bow.dev"))).scalars().first()
        org = (await db.execute(select(Organization))).scalars().first()
        svc = ReportService()
        counter["n"] = 0
        t0 = time.perf_counter()
        res = await svc.get_reports(db, user, org, 1, 50, "my")
        dt = time.perf_counter() - t0
    items = res.get("items", res) if isinstance(res, dict) else res
    try: n_items = len(items)
    except Exception: n_items = "?"
    print(f"reports returned:     {n_items}")
    print(f"wall time:            {dt:.2f}s")
    print(f"SQL statements fired: {counter['n']}")


if __name__ == "__main__":
    asyncio.run(main())

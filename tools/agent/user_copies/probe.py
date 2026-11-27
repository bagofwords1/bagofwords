#!/usr/bin/env python3
"""Ask for an agent's clients as each member and query the cached relation.

Runs in-process against the app database and artifact volume the running
stack uses (the same file locks, the same rows), so it is exactly what a
chat turn does before the model writes any SQL: `construct_clients`, which
builds the asking user's copy when it is missing.

    cd backend && uv run python ../tools/agent/user_copies/probe.py <data_source_id> [email ...]

Prints, per user: how long getting the clients took (a first call includes
the build), which client keys came back, and the cached relation's row count
and amount total next to the same question asked of the live source.

Env: UC_SQL_FAST / UC_SQL_LIVE (the two queries; defaults fit the fixture),
UC_RELATION (sales_cached).
"""
import asyncio
import json
import os
import sys
import time

import main  # noqa: F401  registers every mapper
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.dependencies import async_session_maker
from app.models.data_source import DataSource
from app.models.user import User
from app.services.custom_table_user_copy_service import user_copy_service
from app.services.data_source_service import DataSourceService

RELATION = os.environ.get("UC_RELATION", "sales_cached")
SQL_FAST = os.environ.get("UC_SQL_FAST", f"SELECT count(*) AS n, sum(amount) AS total FROM {RELATION}")
SQL_LIVE = os.environ.get("UC_SQL_LIVE", "SELECT count(*) AS n, sum(amount) AS total FROM sales")


async def one(ds_id: str, email: str) -> dict:
    async with async_session_maker() as db:
        ds = (await db.execute(
            select(DataSource).options(selectinload(DataSource.connections))
            .where(DataSource.id == ds_id))).scalar_one()
        user = (await db.execute(select(User).where(User.email == email))).scalar_one()
        t0 = time.monotonic()
        try:
            clients = await DataSourceService().construct_clients(db, ds, user)
        except Exception as e:
            return {"user": email, "error": f"{type(e).__name__}: {e}"[:300]}
        out = {"user": email, "clients_s": round(time.monotonic() - t0, 2), "keys": sorted(clients)}
        fast = next((c for k, c in clients.items() if k.endswith("::fast")), None)
        live = next((c for k, c in clients.items() if "::" not in k and ":" in k), None)
        if fast is not None:
            t0 = time.monotonic()
            df = await asyncio.to_thread(fast.execute_query, SQL_FAST)
            out["cached"] = {k: (float(v) if v is not None else None) for k, v in df.iloc[0].to_dict().items()}
            out["cached_query_s"] = round(time.monotonic() - t0, 3)
        if live is not None:
            t0 = time.monotonic()
            try:
                df = await asyncio.to_thread(live.execute_query, SQL_LIVE)
                out["live"] = {k: (float(v) if v is not None else None) for k, v in df.iloc[0].to_dict().items()}
            except Exception as e:
                out["live_error"] = str(e)[:200]
            out["live_query_s"] = round(time.monotonic() - t0, 3)
        return out


async def run() -> None:
    ds_id = sys.argv[1]
    emails = sys.argv[2:] or ["demo1@example.com", "demo2@example.com", "demo3@example.com"]
    for email in emails:
        print(json.dumps(await one(ds_id, email), default=str))
    await user_copy_service.wait_for_builds(600)


asyncio.run(run())

#!/usr/bin/env python
"""Seed a network_dir (local directory) file connection + data source + report
in the existing admin org, index it, and print the report id.

network_dir needs no external egress, so it's the reliable in-sandbox way to
drive a real Haiku completion through the file connection tools (search_files /
read_file) and see the model-authored `title`s render in the UI.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx

BASE = "http://localhost:8000"
ROOT = os.environ.get("NETDIR_ROOT", "/tmp/netdir_demo")


def _admin_ctx():
    c = httpx.Client(base_url=BASE, timeout=60)
    tok = c.post("/api/auth/jwt/login", data={"username": "admin@example.com", "password": "Password123!"}).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    org = c.get("/api/organizations", headers=h).json()[0]["id"]
    me = c.get("/api/users/me", headers=h)
    uid = me.json().get("id") if me.status_code == 200 else None
    return org, uid


async def _seed(org_id: str, user_id: str | None) -> str:
    os.environ.setdefault("TESTING", "true")
    import importlib
    import pkgutil
    import app.models as _models_pkg
    for _m in pkgutil.iter_modules(_models_pkg.__path__):
        if _m.name == "application":
            continue
        importlib.import_module(f"app.models.{_m.name}")

    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.connection import Connection
    from app.models.data_source import DataSource
    from app.models.report import Report
    from app.models.user import User
    from app.models.domain_connection import domain_connection
    from app.models.report_data_source_association import report_data_source_association
    from app.services.data_source_service import DataSourceService
    from app.models.organization import Organization

    async with async_session_maker() as db:
        if not user_id:
            user_id = (await db.execute(select(User.id).limit(1))).scalar()

        sfx = os.environ.get("NETDIR_SUFFIX", "2")
        conn = Connection(
            organization_id=org_id, name=f"Contracts Share {sfx}", type="network_dir",
            config={"root_path": ROOT, "recursive": True, "writable": False, "max_file_mb": 50},
            auth_policy="system_only",
        )
        conn.encrypt_credentials({})
        db.add(conn)
        await db.flush()

        ds = DataSource(name=f"Contracts (local dir) {sfx}", organization_id=org_id,
                        is_active=True, owner_user_id=user_id)
        db.add(ds)
        await db.flush()
        await db.execute(domain_connection.insert().values(data_source_id=ds.id, connection_id=conn.id))

        report = Report(title=f"Haiku file-tools title demo {sfx}", slug=f"haiku-file-tools-demo-{sfx}",
                        organization_id=org_id, user_id=user_id)
        db.add(report)
        await db.flush()
        await db.execute(report_data_source_association.insert().values(report_id=report.id, data_source_id=ds.id))
        await db.commit()

        # index (real refresh pipeline)
        org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        await DataSourceService().refresh_data_source_schema(db, str(ds.id), org, user)
        return str(report.id)


def main() -> int:
    from pathlib import Path
    from scripts.gen_network_dir_fixtures import generate
    generate(Path(ROOT))
    org_id, user_id = _admin_ctx()
    rid = asyncio.run(_seed(org_id, user_id))
    print(json.dumps({"report_url": f"http://localhost:3000/reports/{rid}", "report_id": rid}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

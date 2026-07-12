#!/usr/bin/env python
"""Seed a MIXED agent: Chinook (sqlite SQL) + a file connection (network_dir),
to verify the per-connection CatalogSelector (tables grid + file scope together).
"""
from __future__ import annotations
import asyncio, json, os, pkgutil, importlib

NETDIR = "/home/user/bagofwords/.agents/sandbox-feedback-loop/netdir"
CHINOOK = "/home/user/bagofwords/backend/demo-datasources/chinook.sqlite"
EMAIL = "admin@example.com"


async def seed():
    import app.models as _m
    for mod in pkgutil.iter_modules(_m.__path__):
        if mod.name != "application":
            importlib.import_module(f"app.models.{mod.name}")
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.connection import Connection
    from app.models.data_source import DataSource
    from app.models.report import Report
    from app.models.user import User
    from app.models.organization import Organization
    from app.models.domain_connection import domain_connection
    from app.models.report_data_source_association import report_data_source_association
    from app.services.data_source_service import DataSourceService

    async with async_session_maker() as db:
        user = (await db.execute(select(User).where(User.email == EMAIL))).scalar_one()
        org = (await db.execute(select(Organization))).scalars().first()

        ds = DataSource(name="Mixed Agent (Chinook + Files)", organization_id=org.id,
                        is_active=True, owner_user_id=user.id)
        db.add(ds); await db.flush()

        specs = [
            dict(name="Chinook (SQLite)", type="sqlite",
                 config={"database": CHINOOK}, creds={}),
            dict(name="Finance Files", type="network_dir",
                 config={"root_path": NETDIR, "include_globs": "reports/**/*.csv, docs/**",
                         "index_mode": "content", "recursive": True}, creds={}),
        ]
        conn_ids = []
        for s in specs:
            conn = Connection(organization_id=org.id, name=s["name"], type=s["type"],
                              config=s["config"], auth_policy="system_only")
            conn.encrypt_credentials(s["creds"])
            db.add(conn); await db.flush()
            await db.execute(domain_connection.insert().values(data_source_id=ds.id, connection_id=conn.id))
            conn_ids.append((s["name"], str(conn.id)))

        report = Report(title="Mixed agent verification", slug="mixed-agent-verify",
                        organization_id=org.id, user_id=user.id)
        db.add(report); await db.flush()
        await db.execute(report_data_source_association.insert().values(report_id=report.id, data_source_id=ds.id))
        await db.commit()

        await DataSourceService().refresh_data_source_schema(db, str(ds.id), org, user)
        return {"ds_id": str(ds.id), "report_id": str(report.id),
                "wizard_url": f"http://localhost:3000/agents/new/{ds.id}/schema",
                "connections": conn_ids}


if __name__ == "__main__":
    out = asyncio.run(seed())
    print(json.dumps(out, indent=2))
    os.makedirs("/home/user/bagofwords/.agents/sandbox-feedback-loop/runtime", exist_ok=True)
    with open("/home/user/bagofwords/.agents/sandbox-feedback-loop/runtime/seed_mixed.json", "w") as f:
        json.dump(out, f, indent=2)

"""SharePoint 2010 content index → search_files, through the real stack.

Only the SharePoint SOAP server is simulated. The catalog is built by the real
refresh pipeline (ConnectionTable → DataSourceTable) and search_files answers
from it without touching the source for a service-account connection, while a
per-user connection never answers from the shared catalog.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.data_source import DataSource
from app.models.domain_connection import domain_connection
from app.models.organization import Organization
from app.models.report import Report
from app.models.report_data_source_association import report_data_source_association
from app.models.user import User
from tests.unit.test_sharepoint_2010_client import ROOT, SITE, legacy  # noqa: F401


async def _last(agen):
    last = None
    async for ev in agen:
        last = ev
    return last.payload


async def _seed(db, index_mode):
    suffix = uuid.uuid4().hex[:8]
    org = Organization(name=f"SP2010 Org {suffix}")
    db.add(org)
    await db.flush()
    user = User(name="SP User", email=f"sp2010-{suffix}@example.com", hashed_password="x",
                is_active=True, is_superuser=False, is_verified=True)
    db.add(user)
    await db.flush()
    conn = Connection(
        organization_id=org.id, name=f"Legacy library {suffix}", type="sharepoint_onprem",
        config={"site_url": SITE, "drive_name": "Documents", "api_mode": "2010",
                "recursive": True, "index_mode": index_mode},
        auth_policy="system_only",
    )
    conn.encrypt_credentials({"username": "DOMAIN\\reader", "password": "synthetic"})
    db.add(conn)
    await db.flush()
    ds = DataSource(name=f"Legacy DS {suffix}", organization_id=org.id, is_active=True, owner_user_id=user.id)
    db.add(ds)
    await db.flush()
    await db.execute(domain_connection.insert().values(data_source_id=ds.id, connection_id=conn.id))
    report = Report(title=f"Legacy report {suffix}", slug=f"legacy-report-{suffix}",
                    organization_id=org.id, user_id=user.id)
    db.add(report)
    await db.flush()
    await db.execute(report_data_source_association.insert().values(report_id=report.id, data_source_id=ds.id))
    await db.commit()
    return org.id, user.id, conn.id, ds.id, report.id


async def _ctx(db, org_id, user_id, report_id):
    org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    report = (await db.execute(
        select(Report).where(Report.id == report_id)
        .options(selectinload(Report.data_sources).selectinload(DataSource.connections))
    )).scalar_one()
    return {"db": db, "organization": org, "user": user, "report": report}


async def _flow(server):
    from app.ai.tools.implementations.list_files import ListFilesTool
    from app.ai.tools.implementations.search_files import SearchFilesTool
    from app.services.data_source_service import DataSourceService

    async with async_session_maker() as db:
        org_id, user_id, conn_id, ds_id, report_id = await _seed(db, "content")
        ctx = await _ctx(db, org_id, user_id, report_id)
        await DataSourceService().refresh_data_source_schema(db, str(ds_id), ctx["organization"], ctx["user"])
        tables = await DataSourceService().get_data_source_schema(
            db=db, data_source_id=str(ds_id), organization=ctx["organization"],
            current_user=ctx["user"], include_inactive=True,
        )
        catalog = {t.name: (t.metadata_json or {}).get("sharepoint_onprem", {}) for t in tables}
        assert set(catalog) == {p[len(ROOT) + 1:] for p in server.blobs if p.startswith(ROOT + "/")}
        assert all(meta.get("keywords") is not None and meta.get("created_at") for meta in catalog.values())

        # A body-only term is found from the catalog with zero source calls.
        calls_before = len(server.calls)
        payload = await _last(SearchFilesTool().run_stream(
            {"connection_id": str(conn_id), "query": "south region", "max_results": 20}, ctx))
        out = payload["output"]
        assert out["success"] and [f["path"] for f in out["files"]] == ["revenue.csv"]
        assert "keyword index" in payload["observation"]["summary"]
        assert len(server.calls) == calls_before

        # list_files answers from the catalog by default: filters, sort and
        # cursor paging all work without a source call; the result says how
        # fresh the catalog is.
        async def listing(**inputs):
            return await _last(ListFilesTool().run_stream({"connection_id": str(conn_id), **inputs}, ctx))

        calls_before = len(server.calls)
        payload = await listing(recursive=True, limit=2, sort_by="size", sort_order="desc")
        out = payload["output"]
        assert out["success"] and out["source"] == "catalog" and out["indexed_at"]
        assert out["catalog_files"] == len(catalog) == out["matched"]
        assert len(out["files"]) == 2 and out["next_cursor"] and out["incomplete"]
        assert out["files"][0]["size"] >= out["files"][1]["size"]
        assert all(f["created_at"] and f["modified_at"] for f in out["files"])
        assert out["indexed_at"] in payload["observation"]["summary"]
        seen = [f["path"] for f in out["files"]]
        cursor = out["next_cursor"]
        while cursor:
            out = (await listing(recursive=True, limit=2, sort_by="size", sort_order="desc", cursor=cursor))["output"]
            assert out["success"], out
            seen += [f["path"] for f in out["files"]]
            cursor = out["next_cursor"]
        assert sorted(seen) == sorted(catalog) and len(seen) == len(set(seen))
        out = (await listing(name_pattern="*.csv", recursive=True))["output"]
        assert [f["path"] for f in out["files"]] == ["revenue.csv"] and out["matched"] == 1
        out = (await listing(folder_id="nested"))["output"]
        assert [f["path"] for f in out["files"]] == ["nested/דוח.txt"]
        out = (await listing(folder_id=ROOT + "/nested"))["output"]
        assert [f["path"] for f in out["files"]] == ["nested/דוח.txt"]
        out = (await listing(recursive=True, modified_after="2999-01-01"))["output"]
        assert out["success"] and out["files"] == [] and out["matched"] == 0
        assert len(server.calls) == calls_before
        out = (await listing(recursive=True, cursor="stale"))["output"]
        assert not out["success"]
        # Explicit live listing still reaches the source.
        out = (await listing(source="live", recursive=True, limit=50))["output"]
        assert out["success"] and out["source"] == "live" and len(out["files"]) == len(catalog)
        assert len(server.calls) > calls_before

        # The same catalog must never answer for a per-user connection.
        conn = (await db.execute(select(Connection).where(Connection.id == conn_id))).scalar_one()
        conn.auth_policy = "user_required"
        conn.allowed_user_auth_modes = ["ntlm"]
        await db.commit()
        ctx = await _ctx(db, org_id, user_id, report_id)
        calls_before = len(server.calls)
        payload = await _last(SearchFilesTool().run_stream(
            {"connection_id": str(conn_id), "query": "south region", "max_results": 20}, ctx))
        assert "keyword index" not in payload["observation"]["summary"]
        if payload["output"]["success"]:
            # Any answer came from a live crawl under the caller's identity.
            assert len(server.calls) > calls_before
        out = (await listing(source="catalog"))["output"]
        assert not out["success"] and not out.get("files")
        out = (await listing(recursive=True, limit=50))["output"]
        assert out.get("source") != "catalog"


@pytest.mark.e2e
def test_service_account_search_answers_from_content_index(legacy):  # noqa: F811
    asyncio.run(_flow(legacy))

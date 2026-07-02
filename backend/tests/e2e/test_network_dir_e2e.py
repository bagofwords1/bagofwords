"""End-to-end validation of the `network_dir` file connector.

Drives the REAL stack — no mocks below the tool boundary:

    agent tool run_stream
      → resolve_file_client / resolve_file_data_source (report allow-list)
        → ConnectionService.construct_client (decrypts config, instantiates)
          → NetworkDirClient
            → the actual filesystem (a generated fixture tree)

Seeds an org + user + a `network_dir` Connection (writable) + DataSource +
Report exactly like production, then exercises search_files → read_file →
write_file — i.e. the "search contracts, read one, put related files in a
folder" workflow the feature is for.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      python -m pytest tests/e2e/test_network_dir_e2e.py -v -s
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

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

from scripts.gen_network_dir_fixtures import generate


def _run(coro):
    return asyncio.run(coro)


async def _end(agen):
    """Drain a tool run_stream and return the final tool.end output dict."""
    last = None
    async for ev in agen:
        last = ev
    return last.payload["output"]


async def _seed(db, root: str, writable: bool):
    suffix = uuid.uuid4().hex[:8]
    org = Organization(name=f"NetDir Org {suffix}")
    db.add(org)
    await db.flush()

    user = User(
        name="Dir User",
        email=f"diruser-{suffix}@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        is_verified=True,
    )
    db.add(user)
    await db.flush()

    conn = Connection(
        organization_id=org.id,
        name=f"Contracts Share {suffix}",
        type="network_dir",
        config={
            "root_path": root,
            "recursive": True,
            "writable": writable,
            "max_file_mb": 50,
        },
        auth_policy="system_only",
    )
    conn.encrypt_credentials({})  # no-auth connector
    db.add(conn)
    await db.flush()

    ds = DataSource(
        name=f"Contracts DS {suffix}",
        organization_id=org.id,
        is_active=True,
        owner_user_id=user.id,
    )
    db.add(ds)
    await db.flush()
    await db.execute(domain_connection.insert().values(
        data_source_id=ds.id, connection_id=conn.id,
    ))

    report = Report(
        title=f"Contracts Report {suffix}",
        slug=f"contracts-report-{suffix}",
        organization_id=org.id,
        user_id=user.id,
    )
    db.add(report)
    await db.flush()
    await db.execute(report_data_source_association.insert().values(
        report_id=report.id, data_source_id=ds.id,
    ))
    await db.commit()
    return org.id, user.id, conn.id, ds.id, report.id


async def _load_ctx(db, org_id, user_id, report_id):
    """Build a runtime_ctx with the report/ds/connections eager-loaded, as the
    agent runtime provides it."""
    org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    report = (
        await db.execute(
            select(Report)
            .where(Report.id == report_id)
            .options(selectinload(Report.data_sources).selectinload(DataSource.connections))
        )
    ).scalar_one()
    return {"db": db, "organization": org, "user": user, "report": report}


async def _flow(tmp_root: str, writable: bool):
    from app.ai.tools.implementations.search_files import SearchFilesTool
    from app.ai.tools.implementations.read_file import ReadFileTool
    from app.ai.tools.implementations.write_file import WriteFileTool
    from app.ai.tools.implementations.list_files import ListFilesTool

    async with async_session_maker() as db:
        org_id, user_id, conn_id, ds_id, report_id = await _seed(db, tmp_root, writable)
        ctx = await _load_ctx(db, org_id, user_id, report_id)

        # 1. search_files — find contracts by content/filename (live client).
        # "contract" is in every contract file's name, so this is deterministic
        # regardless of the (seeded) random vendor mix.
        out = await _end(SearchFilesTool().run_stream(
            {"connection_id": str(conn_id), "query": "contract", "max_results": 50}, ctx))
        assert out["success"] is True, out
        assert out["file_count"] > 0
        hit = next(f for f in out["files"] if f["id"].endswith(".csv"))

        # 2. read_file — read a matched CSV (agent passes the id it got back).
        out = await _end(ReadFileTool().run_stream(
            {"connection_id": str(conn_id), "file_id": hit["id"]}, ctx))
        assert out["success"] is True, out
        assert out["content_type"] == "tabular"
        assert out["row_count"] >= 1

        # 3. write_file — "put" a generated summary into a new folder.
        out = await _end(WriteFileTool().run_stream(
            {"connection_id": str(conn_id), "filename": "index.md",
             "folder": "_related", "content": "# Related contracts\n- acme\n"}, ctx))
        if writable:
            assert out["success"] is True, out
            assert out["file"]["id"] == "_related/index.md"
            assert Path(tmp_root, "_related", "index.md").exists()
        else:
            # read-only connection: the write tool must be rejected at resolve.
            assert out["success"] is False
            assert "write_file" in out["error"]

    return conn_id


def test_e2e_writable_search_read_write(tmp_path):
    root = tmp_path / "share"
    generate(root, contracts=10, invoices=5, reports=2, images=3)
    _run(_flow(str(root), writable=True))


def test_e2e_readonly_blocks_write(tmp_path):
    root = tmp_path / "share_ro"
    generate(root, contracts=5, invoices=3, reports=1, images=2)
    _run(_flow(str(root), writable=False))

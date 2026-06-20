"""Repro + fix validation: files from a removed agent (data source) must be
removed from the report.

Scenario (from the bug report):
  - A report has multiple "agents" (in this codebase, agents == data sources).
  - One of them is the default ``data_source`` agent which has a *file attached
    to the data source* (here modelled on the chinook demo: a sqlite data source
    with a file in ``data_source_file_association``).
  - When the agents are removed from the report (PUT /reports/{id} with a new,
    smaller data_sources list -> ``set_data_sources_for_report``), the file that
    was snapshotted from the removed data source is NOT removed from the report.

Root cause: ``ReportService.set_data_sources_for_report`` only ever *adds*
data-source files into ``report.files`` and never removes the snapshot rows for
data sources that were dropped.

This is a self-contained SQLite repro following the sandbox-feedback-loop
methodology. It seeds users/orgs/data sources directly (the HTTP signup route is
404 under the sandbox config — a pre-existing, unrelated harness limitation, same
as noted in test_fabric_second_admin_overlay_repro.py) and drives the real
service method that owns the bug.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      python -m pytest tests/e2e/test_report_agent_file_removal_repro.py -v -s
"""
import uuid
import asyncio

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.data_source import DataSource
from app.models.file import File
from app.models.report import Report
from app.models.data_source_file_association import data_source_file_association
from app.models.report_file_association import report_file_association
from app.services.report_service import ReportService


def _run(coro):
    return asyncio.run(coro)


async def _seed_user_org(db, suffix):
    org = Organization(name=f"Agent File Org {suffix}")
    db.add(org)
    await db.flush()
    user = User(
        name="Owner",
        email=f"owner-{suffix}@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        is_verified=True,
    )
    db.add(user)
    await db.flush()
    return org, user


async def _seed_data_source(db, org, user, name):
    ds = DataSource(
        name=name,
        organization_id=org.id,
        is_active=True,
        owner_user_id=user.id,
    )
    db.add(ds)
    await db.flush()
    return ds


async def _seed_file(db, org, user, filename):
    f = File(
        filename=filename,
        path=f"uploads/files/{uuid.uuid4().hex}_{filename}",
        content_type="text/csv",
        user_id=user.id,
        organization_id=org.id,
    )
    db.add(f)
    await db.flush()
    return f


async def _attach_file_to_data_source(db, ds, f):
    await db.execute(
        data_source_file_association.insert().values(
            data_source_id=ds.id, file_id=f.id
        )
    )


async def _seed_report(db, org, user, suffix):
    report = Report(
        title="Multi-agent report",
        slug=f"multi-agent-{suffix}",
        status="draft",
        report_type="regular",
        user_id=user.id,
        organization_id=org.id,
    )
    db.add(report)
    await db.flush()
    return report


async def _report_file_ids(db, report_id):
    res = await db.execute(
        select(report_file_association.c.file_id).where(
            report_file_association.c.report_id == report_id
        )
    )
    return {str(r[0]) for r in res.all()}


@pytest.mark.e2e
def test_removing_agents_removes_their_files_from_report():
    async def _go():
        svc = ReportService()
        suffix = uuid.uuid4().hex[:8]
        async with async_session_maker() as db:
            org, user = await _seed_user_org(db, suffix)
            # Two agents: chinook-like (with an attached file) + a second one.
            chinook = await _seed_data_source(db, org, user, f"Music Store {suffix}")
            stocks = await _seed_data_source(db, org, user, f"Stocks {suffix}")
            agent_file = await _seed_file(db, org, user, "agent_notes.csv")
            await _attach_file_to_data_source(db, chinook, agent_file)
            report = await _seed_report(db, org, user, suffix)
            await db.commit()

            report_id = report.id
            file_id = str(agent_file.id)

            # Add both agents -> the chinook file is snapshotted onto the report.
            await svc.set_data_sources_for_report(db, report, [chinook.id, stocks.id])
            await db.commit()
            assert file_id in await _report_file_ids(db, report_id), (
                "precondition: agent file should be snapshotted onto the report"
            )

            # Remove ALL agents.
            await svc.set_data_sources_for_report(db, report, [])
            await db.commit()

            remaining = await _report_file_ids(db, report_id)
            assert file_id not in remaining, (
                "BUG: file from the removed agent is still attached to the report "
                f"(remaining file ids: {remaining})"
            )

    _run(_go())


@pytest.mark.e2e
def test_directly_uploaded_file_survives_agent_removal():
    """Guardrail: a file the user attached directly to the report (not owned by
    any data source) must survive agent removal."""
    async def _go():
        svc = ReportService()
        suffix = uuid.uuid4().hex[:8]
        async with async_session_maker() as db:
            org, user = await _seed_user_org(db, suffix)
            chinook = await _seed_data_source(db, org, user, f"Music Store {suffix}")
            agent_file = await _seed_file(db, org, user, "agent_notes.csv")
            await _attach_file_to_data_source(db, chinook, agent_file)
            direct_file = await _seed_file(db, org, user, "user_upload.csv")
            report = await _seed_report(db, org, user, suffix)
            # Direct upload: attach straight to the report, no data source.
            await db.execute(
                report_file_association.insert().values(
                    report_id=report.id, file_id=direct_file.id
                )
            )
            await db.commit()

            report_id = report.id
            agent_file_id = str(agent_file.id)
            direct_file_id = str(direct_file.id)

            await svc.set_data_sources_for_report(db, report, [chinook.id])
            await db.commit()

            await svc.set_data_sources_for_report(db, report, [])
            await db.commit()

            remaining = await _report_file_ids(db, report_id)
            assert direct_file_id in remaining, (
                "directly-uploaded file must survive agent removal"
            )
            assert agent_file_id not in remaining, "agent file should be removed"

    _run(_go())


@pytest.mark.e2e
def test_file_shared_by_remaining_agent_survives():
    """Guardrail: a file owned by two agents must stay while one agent remains."""
    async def _go():
        svc = ReportService()
        suffix = uuid.uuid4().hex[:8]
        async with async_session_maker() as db:
            org, user = await _seed_user_org(db, suffix)
            ds_a = await _seed_data_source(db, org, user, f"DS A {suffix}")
            ds_b = await _seed_data_source(db, org, user, f"DS B {suffix}")
            shared_file = await _seed_file(db, org, user, "shared.csv")
            await _attach_file_to_data_source(db, ds_a, shared_file)
            await _attach_file_to_data_source(db, ds_b, shared_file)
            report = await _seed_report(db, org, user, suffix)
            await db.commit()

            report_id = report.id
            shared_file_id = str(shared_file.id)

            await svc.set_data_sources_for_report(db, report, [ds_a.id, ds_b.id])
            await db.commit()

            # Remove only ds_a; ds_b still owns the shared file.
            await svc.set_data_sources_for_report(db, report, [ds_b.id])
            await db.commit()

            remaining = await _report_file_ids(db, report_id)
            assert shared_file_id in remaining, (
                "file still owned by a remaining agent must stay on the report"
            )

    _run(_go())

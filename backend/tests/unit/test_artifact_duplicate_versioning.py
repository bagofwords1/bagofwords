"""Reverting a version numbers it within its own kind, not across the report.

`duplicate` ("Use this version") used to take max(version) over every artifact
of the report, so a document reverted on a report whose dashboard chain had
reached v7 came back as v8 instead of v3 — the doc borrowed a dashboard's
numbering.
"""

# Mapper registration intentionally runs before the app-model imports below.
# ruff: noqa: E402

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Register the same mapped model graph as Alembic before creating the in-memory
# schema, so relationship loaders behave exactly as they do in the app.
_env_src = (Path(__file__).resolve().parents[2] / "alembic" / "env.py").read_text()
for _stmt in re.findall(r"^from app\.models\S* import \([^)]*\)|^from app\.models[^\n]+", _env_src, re.M):
    exec(_stmt)  # noqa: S102 — test-only, mirrors alembic/env.py

from app.models.artifact import Artifact
from app.models.base import Base
from app.models.completion import Completion
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User
from app.services.artifact_service import ArtifactService


@pytest_asyncio.fixture
async def report_context():
    Completion.__table__.c.sigkill.nullable = True
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with maker() as db:
        user = User(name="Owner", email=f"owner-{uuid.uuid4()}@example.test", hashed_password="x")
        db.add(user)
        await db.flush()
        organization = Organization(name=f"Org {uuid.uuid4()}")
        db.add(organization)
        await db.flush()
        report = Report(
            title="Report with a dashboard and a doc",
            slug=f"report-{uuid.uuid4()}",
            user_id=str(user.id),
            organization_id=str(organization.id),
        )
        db.add(report)
        await db.flush()

        def _artifact(title: str, mode: str, version: int, body: str) -> Artifact:
            return Artifact(
                report_id=str(report.id),
                user_id=str(user.id),
                organization_id=str(organization.id),
                title=title,
                mode=mode,
                version=version,
                status="completed",
                # A path whose file does not exist: copy_thumbnail returns None
                # and duplicate() skips the background regeneration task, so the
                # test stays on the numbering and off the screenshot pipeline.
                thumbnail_path=f"thumbnails/{uuid.uuid4()}.png",
                content={"markdown": body} if mode == "doc" else {"code": body, "visualization_ids": []},
            )

        # A dashboard edited seven times...
        for version in range(1, 8):
            db.add(_artifact("Revenue by artist", "page", version, f"page v{version}"))
        # ...and a document that has only ever been edited once.
        doc_v1 = _artifact("Performance report", "doc", 1, "doc v1")
        db.add(doc_v1)
        db.add(_artifact("Performance report", "doc", 2, "doc v2"))
        await db.commit()

        yield db, str(doc_v1.id), str(report.id), str(user.id)

    await engine.dispose()


@pytest.mark.asyncio
async def test_revert_numbers_within_the_artifact_kind(report_context):
    """Reverting the doc to v1 continues the DOC chain: v3, not the page's v8."""
    db, doc_v1_id, _report_id, user_id = report_context

    reverted = await ArtifactService().duplicate(db, doc_v1_id, user_id=user_id)

    assert reverted is not None
    assert reverted.mode == "doc"
    assert reverted.version == 3, (
        f"expected the doc chain to continue at v3, got v{reverted.version} "
        "— the dashboard chain's numbering leaked into the document"
    )
    # The revert is a copy of the version it was taken from.
    assert reverted.content == {"markdown": "doc v1"}


@pytest.mark.asyncio
async def test_revert_never_collides_with_an_existing_version(report_context):
    """Whatever the scope, the new number must be free within its own kind."""
    db, doc_v1_id, report_id, user_id = report_context

    reverted = await ArtifactService().duplicate(db, doc_v1_id, user_id=user_id)

    siblings = [
        a for a in await ArtifactService().list_by_report(db, report_id)
        if a.mode == "doc" and str(a.id) != str(reverted.id)
    ]
    assert reverted.version not in {a.version for a in siblings}

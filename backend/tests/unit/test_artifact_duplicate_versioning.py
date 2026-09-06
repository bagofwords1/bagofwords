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

from app.models.artifact import ArtifactVersion
from app.models.base import Base
from app.models.completion import Completion
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User
from app.services.artifact_service import ArtifactService
from tests.fixtures.artifact import seed_artifact


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

        def _body(mode: str, body: str) -> dict:
            return {"markdown": body} if mode == "doc" else {"code": body, "visualization_ids": []}

        async def _seed(title: str, mode: str, bodies: list[str]):
            return await seed_artifact(
                db,
                report_id=str(report.id),
                user_id=str(user.id),
                organization_id=str(organization.id),
                mode=mode,
                title=title,
                contents=[_body(mode, b) for b in bodies],
                # A path whose file does not exist: copy_thumbnail returns None
                # and duplicate() skips the background regeneration task, so the
                # test stays on the numbering and off the screenshot pipeline.
                thumbnail_path=f"thumbnails/{uuid.uuid4()}.png",
            )

        # A dashboard edited seven times...
        await _seed("Revenue by artist", "page", [f"page v{v}" for v in range(1, 8)])
        # ...and a document that has only ever been edited once.
        await _seed("Performance report", "doc", ["doc v1", "doc v2"])
        versions = await ArtifactService().list_by_report(db, str(report.id))
        doc_v1 = next(a for a in versions if a.mode == "doc" and a.version == 1)
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
async def test_two_same_mode_artifacts_number_independently(report_context):
    """A second dashboard in the same report starts and grows its OWN chain.

    Pre-normalization this was impossible to get right: max(version) could
    only be scoped report+mode, so a second dashboard borrowed the first
    one's numbers. Now the chain is the parent's alone.
    """
    db, _doc_v1_id, report_id, user_id = report_context
    from app.services.artifact_service import new_artifact, new_version

    doc_v1 = await ArtifactService().get(db, _doc_v1_id)
    second = await new_artifact(
        db,
        report_id=report_id,
        organization_id=str(doc_v1.organization_id),
        user_id=user_id,
        mode="page",
        title="Second dashboard",
        content={"code": "function App() {}", "visualization_ids": []},
    )
    assert second.version == 1, (
        f"a NEW dashboard must start at v1 even beside a v7 chain, got v{second.version}"
    )

    grown = await new_version(db, second, user_id=user_id, content={"code": "x", "visualization_ids": []})
    assert grown.version == 2
    assert grown.artifact_id == second.artifact_id


@pytest.mark.asyncio
async def test_soft_deleted_top_version_number_is_never_reissued(report_context):
    """Soft-deleting the newest version must not free its number — the
    UNIQUE(artifact_id, version) spans deleted rows, so reissuing it would
    be an IntegrityError (or a silent history rewrite)."""
    db, doc_v1_id, _report_id, user_id = report_context
    from datetime import datetime

    from app.services.artifact_service import new_version

    doc_v1 = await ArtifactService().get(db, doc_v1_id)
    doc_v3 = await new_version(db, doc_v1, user_id=user_id, content={"markdown": "v3"})
    doc_v3.deleted_at = datetime.utcnow()
    await db.commit()

    after_delete = await new_version(db, doc_v1, user_id=user_id, content={"markdown": "next"})
    assert after_delete.version == 4, (
        f"expected v4 (v3 is soft-deleted but its number stays reserved), got v{after_delete.version}"
    )


@pytest.mark.asyncio
async def test_rename_via_new_version_retitles_only_that_artifact(report_context):
    """title lives on the parent: a rename applies to every version of THAT
    artifact and to no other artifact of the report."""
    db, doc_v1_id, report_id, user_id = report_context
    from app.services.artifact_service import new_version

    doc_v1 = await ArtifactService().get(db, doc_v1_id)
    renamed = await new_version(
        db, doc_v1, user_id=user_id, content={"markdown": "v3"}, title="Renamed report"
    )
    await db.commit()

    # A fresh request sees the rename immediately; inside this session the
    # pre-rename instances sit in the identity map with their stale loaded
    # title, so expire them the way a new request's empty session would.
    db.expire_all()
    listed = await ArtifactService().list_by_report(db, report_id)
    doc_titles = {a.title for a in listed if a.mode == "doc"}
    page_titles = {a.title for a in listed if a.mode == "page"}
    assert doc_titles == {"Renamed report"}, (
        "every version of the renamed doc reads the parent's new title"
    )
    assert page_titles == {"Revenue by artist"}, "the dashboard must keep its own title"
    assert renamed.title == "Renamed report"


def test_constructor_refuses_title_and_mode():
    """column_property would swallow these silently — fail loudly instead."""
    with pytest.raises(TypeError, match="parent Artifact"):
        ArtifactVersion(title="sneaky")
    with pytest.raises(TypeError, match="parent Artifact"):
        ArtifactVersion(mode="page")


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

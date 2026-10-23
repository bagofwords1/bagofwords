"""edit_artifact's concurrency guard counts this conversation's versions only.

Several conversations edit one dashboard (collab01): each adds versions of the
same artifact. The guard ("a newer UI version exists, read it first") is about
the conversation the agent read -- another editor's versions are not a
conflict, or every edit after a teammate's would be refused once.
"""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.ai.tools.implementations.edit_artifact import latest_version_in_conversation
from app.models.base import Base
from app.models.completion import Completion
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User
from app.services.artifact_service import new_version
from tests.fixtures.artifact import seed_artifact


@pytest_asyncio.fixture
async def db():
    Completion.__table__.c.sigkill.nullable = True
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _report(db, user, org, title):
    report = Report(title=title, slug=f"r-{uuid.uuid4()}", user_id=str(user.id), organization_id=str(org.id))
    db.add(report)
    await db.flush()
    return report


@pytest.mark.asyncio
async def test_other_conversations_versions_are_not_a_conflict(db):
    dana = User(name="Dana", email=f"dana-{uuid.uuid4()}@example.test", hashed_password="x")
    avi = User(name="Avi", email=f"avi-{uuid.uuid4()}@example.test", hashed_password="x")
    org = Organization(name=f"org-{uuid.uuid4()}")
    db.add_all([dana, avi, org])
    await db.flush()
    danas, avis = await _report(db, dana, org, "Dana's"), await _report(db, avi, org, "Avi's")

    v2 = await seed_artifact(db, report_id=str(danas.id), user_id=str(dana.id), organization_id=str(org.id),
                             contents=[{"code": "function App() {}"}, {"code": "function App() { return 1 }"}])
    v3 = await new_version(db, v2, content={"code": "function App() { return 2 }"},
                           user_id=str(avi.id), report_id=str(avis.id))  # Avi joins
    assert v3.version == 3
    # Dana's conversation read v2 and nothing newer of its own exists.
    assert await latest_version_in_conversation(db, v2) == 2
    assert await latest_version_in_conversation(db, v3) == 3
    # A newer version in her own conversation is still a conflict.
    v4 = await new_version(db, v2, content={"code": "function App() { return 3 }"})
    assert await latest_version_in_conversation(db, v2) == v4.version == 4

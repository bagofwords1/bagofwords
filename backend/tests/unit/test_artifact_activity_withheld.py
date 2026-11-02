"""Events a restricted automatic check withheld never reach the agent: not by
read_artifact_activity (listed or by id) and not by the per-turn
<app_activity> section. The editor's activity log still lists them, and other
events are unchanged."""
# ruff: noqa: E402
from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

_env_src = (Path(__file__).resolve().parents[2] / "alembic" / "env.py").read_text()
for _stmt in re.findall(r"^from app\.models\S* import \([^)]*\)|^from app\.models[^\n]+", _env_src, re.M):
    exec(_stmt)  # noqa: S102 -- test-only, mirrors alembic/env.py verbatim

from app.dependencies import async_session_maker
from app.models.artifact_activity import ArtifactActivityEvent
from app.models.organization import Organization
from app.services import artifact_activity

pytestmark = pytest.mark.db

SECRET = "TypeError: secret-row-078-05-1120"


@pytest_asyncio.fixture
async def db():
    async with async_session_maker() as session:
        yield session
        await session.rollback()


async def _seed(db):
    organization = Organization(name=f"Withheld org {uuid.uuid4()}")
    db.add(organization)
    await db.flush()
    report_id, artifact_id = str(uuid.uuid4()), str(uuid.uuid4())

    def event(session_id, seq, message, *, shown=None):
        return ArtifactActivityEvent(
            organization_id=str(organization.id), artifact_id=artifact_id, report_id=report_id,
            session_id=session_id, seq=seq, source="editor", kind="error", status="error",
            detail={"message": message}, shown_in_completion_id=shown)

    check, editor = f"preview-{uuid.uuid4().hex[:12]}", f"editor-{uuid.uuid4().hex[:12]}"
    withheld_new = event(check, 1, SECRET)
    withheld_shown = event(check, 2, SECRET + " again", shown=str(uuid.uuid4()))  # stamped before
    visible = event(editor, 1, "TypeError: the editor's own crash")
    db.add_all([withheld_new, withheld_shown, visible])
    await db.commit()
    await artifact_activity.withhold(db, session_id=check)
    return report_id, withheld_new.id, withheld_shown.id, visible.id


@pytest.mark.asyncio
async def test_withheld_events_never_reach_the_agent(db):
    report_id, withheld_new, withheld_shown, visible = await _seed(db)
    async with async_session_maker() as fresh:  # persisted, not just in this session
        listed = await artifact_activity.read(fresh, report_id=report_id)
        assert [e["id"] for e in listed["events"]] == [visible]
        assert SECRET not in str(listed)
        for event_id in (withheld_new, withheld_shown):
            by_id = await artifact_activity.read(fresh, report_id=report_id, event_id=event_id)
            assert by_id == {"events": [], "error": "Event not found in this report"}
        section = await artifact_activity.context_section(fresh, report_id=report_id, mark=False)
        assert section is not None and "editor's own crash" in section and "secret" not in section


@pytest.mark.asyncio
async def test_the_editors_log_still_lists_withheld_events(db):
    report_id, withheld_new, withheld_shown, visible = await _seed(db)
    async with async_session_maker() as fresh:
        ids = [e.id for e in await artifact_activity.recent(fresh, report_id=report_id)]
    assert set(ids) == {withheld_new, withheld_shown, visible}


@pytest.mark.asyncio
async def test_marking_shown_never_unwithholds(db):
    report_id, withheld_new, _withheld_shown, visible = await _seed(db)
    session_of = {}
    async with async_session_maker() as fresh:
        for e in await artifact_activity.recent(fresh, report_id=report_id):
            session_of[e.id] = e.session_id
        await artifact_activity.mark_shown(fresh, session_id=session_of[withheld_new], completion_id=str(uuid.uuid4()))
        await artifact_activity.context_section(fresh, report_id=report_id, completion_id=str(uuid.uuid4()))
        event = await fresh.get(ArtifactActivityEvent, withheld_new)
        assert event.shown_in_completion_id == artifact_activity.WITHHELD

"""A rejected resource change is a tool result, never a crashed agent turn.

Authoring tools share the agent's database session. A rejected schema change,
rebuild must roll back only its own work: the agent's loaded
objects stay usable, the error reaches the model as an observation, and the
next tool call in the same turn succeeds. (Live E5 eval: a session-wide
rollback expired the agent's execution row and every later step raised
MissingGreenlet, turning ordinary 409s into HTTP 500s.)

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/test_artifact_resource_tool_errors.py --db=sqlite
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.artifact_resource import ArtifactResource
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User
from tests.fixtures.artifact import seed_artifact
from sqlalchemy import select


def _definition(name, **fields):
    return {"name": name, "kind": "collection",
            "fields": fields or {"title": {"type": "string", "required": True}}}


@pytest.fixture
def report_ctx(monkeypatch, create_user, login_user, whoami, create_report, test_client):
    monkeypatch.setenv("BOW_ARTIFACT_RESOURCES_ENABLED", "true")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org = whoami(token)["organizations"][0]["id"]
    report = create_report(user_token=token, org_id=org)
    # Stands in for the agent's own loaded rows (execution, plan): state no
    # authoring tool reads, so nothing incidentally reloads it.
    agent_state = create_report(user_token=token, org_id=org)

    async def seed():
        async with async_session_maker() as db:
            row = await db.get(Report, report["id"])
            version = await seed_artifact(db, report_id=row.id, user_id=row.user_id, organization_id=org)
            await db.commit()
            return str(version.artifact_id), str(version.id)

    artifact_id, version_id = asyncio.run(seed())
    return (report["id"], agent_state["id"]), artifact_id, version_id


async def _tool_end(tool, tool_input, ctx):
    ends = [e async for e in tool.run_stream(tool_input, ctx) if e.type == "tool.end"]
    assert ends, "tool emitted no tool.end"
    return ends[-1].payload


async def _turn(ids, steps):
    """Run tool calls in ONE session, like one agent turn, touching the
    agent's loaded state between calls as the agent loop does."""
    report_id, state_id = ids
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        agent_state = await db.get(Report, state_id)
        ctx = {"db": db, "report": report,
               "user": await db.get(User, report.user_id),
               "organization": await db.get(Organization, report.organization_id)}
        results = []
        for tool, tool_input in steps:
            results.append(await _tool_end(tool, tool_input, ctx))
            # Plain attribute access on already-loaded state; raises
            # MissingGreenlet if the tool expired the shared session.
            assert agent_state.title is not None and agent_state.id == state_id
        return results


async def _live_names(artifact_id):
    async with async_session_maker() as db:
        rows = await db.scalars(select(ArtifactResource).where(
            ArtifactResource.artifact_id == artifact_id, ArtifactResource.deleted_at.is_(None)))
        return {r.name for r in rows}


def _manage(artifact_id, **change):
    from app.ai.tools.implementations.manage_artifact_resources import ManageArtifactResources
    return ManageArtifactResources(), {"artifact_id": artifact_id,
                                       "idempotency_key": "k-" + uuid.uuid4().hex, **change}


@pytest.mark.e2e
@pytest.mark.parametrize("rejected", ["duplicate_name", "stale_revision", "missing_resource"])
def test_rejected_schema_change_keeps_the_turn_alive(report_ctx, rejected):
    ids, artifact_id, _ = report_ctx
    first = f"items_{uuid.uuid4().hex[:6]}"
    later = f"notes_{uuid.uuid4().hex[:6]}"
    bad = {
        "duplicate_name": dict(action="create", definition=_definition(first)),
        "stale_revision": dict(action="update", resource=first, expected_revision=99,
                               definition=_definition(first, title={"type": "string"}, body={"type": "string"})),
        "missing_resource": dict(action="delete", resource="never_created", expected_revision=1),
    }[rejected]

    created, refused, recovered = asyncio.run(_turn(ids, [
        _manage(artifact_id, action="create", definition=_definition(first)),
        _manage(artifact_id, **bad),
        _manage(artifact_id, action="create", definition=_definition(later)),
    ]))

    assert "error" not in created["observation"], created
    assert refused["output"]["success"] is False
    assert refused["output"]["committed"] is False
    assert refused["observation"]["error_code"].startswith("ARTIFACT_RESOURCE_")
    assert refused["observation"]["error"], "the rejection must reach the model as an observation"
    assert "error" not in recovered["observation"], "a later call in the same turn must still work"
    assert asyncio.run(_live_names(artifact_id)) == {first, later}




@pytest.fixture
def clean_render(monkeypatch):
    """Skip Playwright: these tests are about the shared session, not rendering."""
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool

    async def _clean(self, code, artifact_data, mode, runtime_ctx, **kwargs):
        yield {"code": code, "clean": True, "screenshot": None, "errors": [], "repair_attempts": 0}

    monkeypatch.setattr(CreateArtifactTool, "_validate_and_repair_stream", _clean)


@pytest.mark.e2e
def test_rejected_rebuild_definition_keeps_the_turn_alive(report_ctx, clean_render):
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool

    ids, _, _ = report_ctx
    name = f"tasks_{uuid.uuid4().hex[:6]}"
    code = '<script type="text/babel">function App() { return null }</script>'
    build = {"prompt": "tracker", "mode": "page", "code": code, "title": "Tracker",
             "resources": [_definition(name)]}
    first = asyncio.run(_turn(ids, [(CreateArtifactTool(), build)]))[0]
    assert "error" not in first["observation"], first
    version_id = first["output"]["artifact_id"]

    # Same turn: a rebuild that silently changes the schema is refused, and the
    # agent can still make an explicit resource change afterwards.
    changed = {**build, "replaces_artifact_id": version_id,
               "resources": [_definition(name, title={"type": "string"}, hours={"type": "number"})]}
    async def resource_parent():
        from app.models.artifact import ArtifactVersion
        async with async_session_maker() as db:
            return str((await db.get(ArtifactVersion, version_id)).artifact_id)
    artifact_id = asyncio.run(resource_parent())
    later = f"notes_{uuid.uuid4().hex[:6]}"
    refused, recovered = asyncio.run(_turn(ids, [
        (CreateArtifactTool(), changed),
        _manage(artifact_id, action="create", definition=_definition(later)),
    ]))

    assert refused["output"] == {"success": False} and refused["observation"]["error"]
    assert "error" not in recovered["observation"]
    assert asyncio.run(_live_names(artifact_id)) == {name, later}

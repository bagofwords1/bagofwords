"""A UI version's resource requirements track the code it actually runs.

Publication refuses a version whose code needs a resource that no longer
matches, so the requirements must cover every resource the code names,
including ones added after the first build (live E5 eval: a collection added
with manage_artifact_resources and wired in by edit_artifact was missing, and
the version published after that collection was deleted).

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/test_artifact_resource_requirements.py --db=sqlite
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.report import Report
from tests.fixtures.artifact import seed_artifact


def _collection(name):
    return {"name": name, "kind": "collection", "fields": {"title": {"type": "string"}, "done": {"type": "boolean"}}}


@pytest.fixture
def app_ctx(monkeypatch, test_client, create_user, login_user, whoami, create_report):
    monkeypatch.setenv("BOW_ARTIFACT_RESOURCES_ENABLED", "true")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org = whoami(token)["organizations"][0]["id"]
    report = create_report(user_token=token, org_id=org)
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org}
    return test_client, headers, report["id"], org


def _seed(report_id, org, code, requirements):
    async def run():
        async with async_session_maker() as db:
            row = await db.get(Report, report_id)
            version = await seed_artifact(
                db, report_id=row.id, user_id=row.user_id, organization_id=org,
                content={"code": code, "sdk_version": 1, "resource_requirements": requirements},
            )
            await db.commit()
            return str(version.artifact_id), str(version.id)
    return asyncio.run(run())


def _configure(client, base, headers, **change):
    return client.post(base + "/resources", headers=headers,
                       json={"idempotency_key": "k-" + uuid.uuid4().hex, **change})


def _publish(client, base, headers, version_id):
    revision = client.get(base + "/publication", headers=headers).json()["revision"]
    return client.post(base + "/publication", headers=headers, json={
        "version_id": version_id, "expected_revision": revision, "idempotency_key": "k-" + uuid.uuid4().hex})


@pytest.mark.e2e
def test_requirements_cover_every_named_resource_and_only_those(app_ctx):
    from app.services.artifact_resource_service import code_requirements

    client, headers, report_id, org = app_ctx
    used, added, unused = (f"{p}_{uuid.uuid4().hex[:6]}" for p in ("tasks", "checklist", "archive"))
    artifact_id, _ = _seed(report_id, org, f"collection('{used}')", {})
    base = f"/api/artifacts/{artifact_id}/runtime"
    for name in (used, added, unused):
        assert _configure(client, base, headers, action="create", definition=_collection(name)).status_code == 200

    code = f'const a = BOW.collection("{used}"); const b = BOW.collection(`{added}`);'

    async def compute():
        async with async_session_maker() as db:
            return await code_requirements(db, artifact_id, code, {})
    requirements = asyncio.run(compute())

    assert set(requirements) == {used, added}
    assert requirements[added] == {"kind": "collection", "fields": {"title": "string", "done": "boolean"}}


@pytest.mark.e2e
def test_publishing_a_version_whose_resource_was_deleted_is_refused(app_ctx):
    client, headers, report_id, org = app_ctx
    name = f"checklist_{uuid.uuid4().hex[:6]}"
    requirement = {name: {"kind": "collection", "fields": {"title": "string", "done": "boolean"}}}
    artifact_id, version_id = _seed(report_id, org, f"collection('{name}')", requirement)
    base = f"/api/artifacts/{artifact_id}/runtime"
    created = _configure(client, base, headers, action="create", definition=_collection(name)).json()

    assert _publish(client, base, headers, version_id).status_code == 200
    deleted = _configure(client, base, headers, action="delete", resource=name, expected_revision=created["revision"])
    assert deleted.status_code == 200, deleted.text

    refused = _publish(client, base, headers, version_id)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "ARTIFACT_RESOURCE_CONFLICT"


@pytest.mark.e2e
def test_reused_deleted_name_is_reported_differently_from_a_live_duplicate(app_ctx):
    client, headers, report_id, org = app_ctx
    artifact_id, _ = _seed(report_id, org, "", {})
    base = f"/api/artifacts/{artifact_id}/runtime"
    gone, live = (f"{p}_{uuid.uuid4().hex[:6]}" for p in ("gone", "live"))
    created = _configure(client, base, headers, action="create", definition=_collection(gone)).json()
    _configure(client, base, headers, action="delete", resource=gone, expected_revision=created["revision"])
    assert _configure(client, base, headers, action="create", definition=_collection(live)).status_code == 200

    reused = _configure(client, base, headers, action="create", definition=_collection(gone))
    duplicate = _configure(client, base, headers, action="create", definition=_collection(live))

    for response in (reused, duplicate):
        assert response.status_code == 409
        assert response.json()["error_code"] == "ARTIFACT_RESOURCE_CONFLICT"
    # The two cases need different recoveries (pick a new name vs. update), so
    # the caller must be able to tell them apart.
    assert reused.json()["detail"] != duplicate.json()["detail"]
    assert gone in reused.json()["detail"] and live in duplicate.json()["detail"]

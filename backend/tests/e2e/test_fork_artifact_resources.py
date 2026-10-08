"""Forking a data app carries its resource definitions, never its records."""

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.artifact import Artifact
from tests.e2e.test_artifact_resources import artifact_api as artifact_api, make_collection


def _fork_artifact_id(fork_id):
    async def load():
        async with async_session_maker() as db:
            return await db.scalar(select(Artifact.id).where(Artifact.report_id == fork_id))
    return asyncio.run(load())


@pytest.mark.e2e
def test_fork_copies_resource_definitions_but_no_records(artifact_api):
    client, base, headers, report_id = artifact_api
    name = f"items_{uuid.uuid4().hex[:6]}"
    make_collection(client, base, headers, name=name)
    created = client.post(f"{base}/collections/{name}/records", headers=headers, json={
        "action": "create", "data": {"title": "secret"}, "idempotency_key": str(uuid.uuid4())})
    assert created.status_code == 200, created.text

    assert client.post(f"/api/reports/{report_id}/publish", headers=headers).status_code == 200
    fork = client.post(f"/api/reports/{report_id}/fork", headers=headers, json={})
    assert fork.status_code == 200, fork.text
    fork_artifact = _fork_artifact_id(fork.json()["id"])
    assert fork_artifact and fork_artifact not in base
    fork_base = f"/api/artifacts/{fork_artifact}/runtime"

    source_defs = {d["name"]: d for d in client.get(base + "/resources", headers=headers).json()["items"]}
    fork_defs = {d["name"]: d for d in client.get(fork_base + "/resources", headers=headers).json()["items"]}
    assert set(fork_defs) == set(source_defs)
    assert fork_defs[name]["fields"] == source_defs[name]["fields"]
    assert fork_defs[name]["permissions"] == source_defs[name]["permissions"]

    # The app's code keeps working against the fork's own, empty storage.
    listed = client.post(f"{fork_base}/collections/{name}/records", headers=headers, json={"action": "list"})
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"] == []
    write = client.post(f"{fork_base}/collections/{name}/records", headers=headers, json={
        "action": "create", "data": {"title": "mine"}, "idempotency_key": str(uuid.uuid4())})
    assert write.status_code == 200, write.text

    # And the source is untouched by the fork's writes.
    source = client.post(f"{base}/collections/{name}/records", headers=headers, json={"action": "list"}).json()
    assert [r["data"]["title"] for r in source["items"]] == ["secret"]

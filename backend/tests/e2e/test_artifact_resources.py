"""Resource HTTP contracts: tenancy, policy, durable records and safe retries."""

import asyncio
import uuid
import pytest
from app.dependencies import async_session_maker
from app.models.report import Report
from tests.fixtures.artifact import seed_artifact

# Explicit re-exports register shared fixtures with pytest.
from tests.e2e.rbac.conftest import (
    invite_user_to_org as invite_user_to_org,
    create_group as create_group,
    add_user_to_group as add_user_to_group,
    enterprise_license as enterprise_license,
)


@pytest.fixture
def artifact_api(monkeypatch, test_client, create_user, login_user, whoami, create_report):
    monkeypatch.delenv("BOW_ARTIFACT_RESOURCES_ENABLED", raising=False)
    user = create_user()
    token = login_user(user["email"], user["password"])
    org = whoami(token)["organizations"][0]["id"]
    report = create_report(user_token=token, org_id=org)

    async def seed():
        async with async_session_maker() as db:
            row = await db.get(Report, report["id"])
            artifact = await seed_artifact(db, report_id=row.id, user_id=row.user_id, organization_id=org)
            await db.commit()
            return artifact.artifact_id

    artifact = asyncio.run(seed())
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org}
    settings = test_client.get('/api/organization/settings', headers=headers)
    assert settings.status_code == 200
    assert settings.json()['config']['enable_artifact_resources']['value'] is False
    assert test_client.get(f'/api/artifacts/{artifact}/runtime/resources', headers=headers).status_code == 404
    assert test_client.put('/api/organization/settings', headers=headers, json={
        'config': {'enable_artifact_resources': {'value': True}}}).status_code == 200
    return test_client, f"/api/artifacts/{artifact}/runtime", headers, report["id"]


def make_collection(client, base, headers, **overrides):
    definition = {
        "name": "entries",
        "fields": {
            "title": {"type": "string", "required": True},
            "slug": {"type": "string", "indexed": True, "unique": True},
            "status": {"type": "string", "default": "draft", "indexed": True},
        },
        **overrides,
    }
    result = client.post(
        base + "/resources",
        headers=headers,
        json={"action": "create", "definition": definition, "idempotency_key": str(uuid.uuid4())},
    )
    assert result.status_code == 200, result.text
    return result.json()


def records(client, base, headers, **payload):
    return client.post(base + "/collections/entries/records", headers=headers, json=payload)


@pytest.mark.e2e
def test_records_validate_paginate_conflict_and_deduplicate(artifact_api):
    client, base, headers, _ = artifact_api
    make_collection(client, base, headers)
    key = str(uuid.uuid4())
    created = records(
        client, base, headers, action="create", data={"title": "First", "slug": "one"}, idempotency_key=key
    )
    assert created.status_code == 200, created.text
    assert (
        records(
            client, base, headers, action="create", data={"title": "First", "slug": "one"}, idempotency_key=key
        ).json()
        == created.json()
    )
    assert (
        records(
            client,
            base,
            headers,
            action="create",
            data={"title": "Another", "slug": "one"},
            idempotency_key=str(uuid.uuid4()),
        ).status_code
        == 409
    )
    for number in range(3):
        response = records(
            client, base, headers, action="create", data={"title": str(number)}, idempotency_key=str(uuid.uuid4())
        )
        assert response.status_code == 200, response.text
    page = records(client, base, headers, action="list", limit=2).json()
    second = records(client, base, headers, action="list", limit=2, cursor=page["nextCursor"]).json()
    assert len(page["items"]) == len(second["items"]) == 2
    assert not {x["id"] for x in page["items"]} & {x["id"] for x in second["items"]}
    record = created.json()
    edit = dict(
        action="update",
        id=record["id"],
        expected_revision=record["revision"],
        data={"title": "Changed"},
        idempotency_key=str(uuid.uuid4()),
    )
    assert records(client, base, headers, **edit).status_code == 200
    edit["idempotency_key"] = str(uuid.uuid4())
    assert records(client, base, headers, **edit).status_code == 409
    assert (
        records(
            client, base, headers, action="create", data={"title": None}, idempotency_key=str(uuid.uuid4())
        ).status_code
        == 400
    )
    assert records(client, base, {**headers, "X-Organization-Id": str(uuid.uuid4())}, action="list").status_code == 404


@pytest.mark.e2e
def test_public_policy_applies_to_get_and_list_and_never_writes(artifact_api):
    client, base, headers, report_id = artifact_api
    make_collection(
        client,
        base,
        headers,
        permissions={"read": {"audience": "public", "equals": {"status": "published"}, "fields": ["title", "status"]}},
    )
    private = records(
        client, base, headers, action="create", data={"title": "Private"}, idempotency_key=str(uuid.uuid4())
    ).json()
    public = records(
        client,
        base,
        headers,
        action="create",
        data={"title": "Public", "status": "published"},
        idempotency_key=str(uuid.uuid4()),
    ).json()
    assert (
        client.put(
            f"/api/reports/{report_id}/visibility/artifact", headers=headers, json={"visibility": "public"}
        ).status_code
        == 200
    )
    page = records(client, base, {}, action="list")
    assert page.status_code == 200, page.text
    assert [item["id"] for item in page.json()["items"]] == [public["id"]]
    assert records(client, base, {}, action="get", id=private["id"]).status_code == 404
    assert (
        records(
            client, base, {}, action="create", data={"title": "intrusion"}, idempotency_key=str(uuid.uuid4())
        ).status_code
        == 403
    )
    assert client.get(base + "/analytics").status_code == 403


@pytest.mark.e2e
def test_deleted_record_retry_and_resource_revision(artifact_api):
    client, base, headers, _ = artifact_api
    resource = make_collection(client, base, headers)
    created = records(
        client, base, headers, action="create", data={"title": "Remove"}, idempotency_key=str(uuid.uuid4())
    ).json()
    payload = dict(
        action="delete", id=created["id"], expected_revision=created["revision"], idempotency_key=str(uuid.uuid4())
    )
    deleted = records(client, base, headers, **payload)
    assert deleted.status_code == 200, deleted.text
    assert records(client, base, headers, **payload).json() == deleted.json()
    response = client.post(
        base + "/resources",
        headers=headers,
        json={
            "action": "delete",
            "resource": resource["id"],
            "expected_revision": resource["revision"] + 1,
            "idempotency_key": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 409


@pytest.mark.e2e
def test_schema_evolution_preserves_records_and_rejects_destructive_change(artifact_api):
    client, base, headers, _ = artifact_api
    resource = make_collection(client, base, headers)
    saved = records(
        client, base, headers, action="create", data={"title": "Keep this"}, idempotency_key=str(uuid.uuid4())
    ).json()
    definition = client.get(base + "/resources", headers=headers).json()["items"][0]
    definition.pop("id")
    definition.pop("revision")
    definition["fields"]["category"] = {"type": "string"}
    payload = {
        "action": "update",
        "resource": resource["id"],
        "definition": definition,
        "expected_revision": 1,
        "idempotency_key": str(uuid.uuid4()),
    }
    response = client.post(base + "/resources", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert records(client, base, headers, action="get", id=saved["id"]).json()["data"]["title"] == "Keep this"
    del definition["fields"]["title"]
    payload.update(expected_revision=2, idempotency_key=str(uuid.uuid4()))
    assert client.post(base + "/resources", headers=headers, json=payload).status_code == 409


@pytest.mark.e2e
def test_files_are_private_encrypted_and_reference_safe(artifact_api, monkeypatch, tmp_path):
    client, base, headers, _ = artifact_api
    monkeypatch.setenv("BOW_ARTIFACT_STORAGE", str(tmp_path))
    response = client.post(
        base + "/resources",
        headers=headers,
        json={
            "action": "create",
            "idempotency_key": str(uuid.uuid4()),
            "definition": {"name": "documents", "kind": "files"},
        },
    )
    assert response.status_code == 200, response.text
    raw = b"A private document for this test."
    response = client.post(
        base + "/files/documents/upload", headers=headers, files={"file": ("sample.txt", raw, "text/plain")}
    )
    assert response.status_code == 200, response.text
    file = response.json()
    assert client.get(base + "/files/" + file["id"] + "/content", headers=headers).content == raw
    assert all(raw not in path.read_bytes() for path in tmp_path.glob("*.blob"))
    assert client.get(base + "/files/" + file["id"] + "/content").status_code in (403, 404)
    assert (
        client.post(
            base + "/files/documents/upload",
            headers=headers,
            files={"file": ("fake.png", b"not an image", "image/png")},
        ).status_code
        == 400
    )
    make_collection(client, base, headers, fields={"attachment": {"type": "file"}})
    result = records(
        client, base, headers, action="create", data={"attachment": file["id"]}, idempotency_key=str(uuid.uuid4())
    )
    assert result.status_code == 200, result.text
    assert client.delete(base + "/files/" + file["id"], headers=headers).status_code == 409
    row = result.json()
    assert (
        records(
            client,
            base,
            headers,
            action="delete",
            id=row["id"],
            expected_revision=row["revision"],
            idempotency_key=str(uuid.uuid4()),
        ).status_code
        == 200
    )
    assert client.delete(base + "/files/" + file["id"], headers=headers).status_code == 200
    assert client.get(base + "/files/" + file["id"] + "/content", headers=headers).status_code == 404
    assert not list(tmp_path.glob("*.blob"))


@pytest.mark.e2e
def test_views_deduplicate_and_request_sizes_are_bounded(artifact_api):
    client, base, headers, _ = artifact_api
    token = client.get(base + "/view-token?surface=embedded", headers=headers).json()
    for _ in range(2):
        assert client.post(base + "/views", headers=headers, json=token).status_code == 200
    analytics = client.get(base + "/analytics", headers=headers).json()
    assert analytics["views"] == 1
    assert analytics["authenticatedViewers"] == 1
    response = client.post(
        base + "/collections/entries/records",
        headers=headers,
        json={"action": "create", "data": {"text": "a" * 524288}},
    )
    assert response.status_code == 413


@pytest.mark.e2e
def test_retired_publication_pins_do_not_restrict_shared_history(artifact_api):
    client, base, headers, report_id = artifact_api

    async def versions():
        from app.models.artifact import ArtifactVersion
        from app.services.artifact_service import new_version
        from sqlalchemy import select

        async with async_session_maker() as db:
            row = await db.scalar(select(ArtifactVersion).where(ArtifactVersion.report_id == report_id))
            draft = await new_version(db, row, user_id=row.user_id, content={"code": "draft"})
            report = await db.get(Report, report_id)
            report.artifact_visibility = "public"
            # A retired pin cannot be produced through the API anymore.
            from app.models.artifact_resource import ArtifactPublication
            db.add(ArtifactPublication(artifact_id=row.artifact_id, version_id=row.id, revision=1))
            await db.commit()
            return row.id, draft.id

    first, second = asyncio.run(versions())

    # Public service is the existing sharing boundary, not a parallel share model.
    async def shared():
        from app.services.report_service import ReportService

        async with async_session_maker() as db:
            return [a.id for a in await ReportService().get_public_artifacts(db, report_id)]

    assert set(asyncio.run(shared())) == {first, second}
    for version_id in (first, second):
        response = client.get(f"/api/r/{report_id}/artifacts/{version_id}")
        assert response.status_code == 200, response.text
        assert response.json()["id"] == version_id
    assert client.post(base + "/publication", headers=headers, json={}).status_code == 404
    from app.ai.tools.mcp import list_mcp_tools
    assert 'publish_artifact' not in {t['name'] for t in list_mcp_tools()}


@pytest.mark.e2e
def test_member_ownership_and_publish_field_survive_policy_changes(artifact_api, invite_user_to_org):
    client, base, headers, report_id = artifact_api
    member = invite_user_to_org(org_id=headers["X-Organization-Id"], admin_token=headers["Authorization"].split()[1])
    member_headers = {**headers, "Authorization": "Bearer " + member["token"]}
    assert (
        client.put(
            f"/api/reports/{report_id}/visibility/artifact", headers=headers, json={"visibility": "public"}
        ).status_code
        == 200
    )
    permissions = {op: {"audience": "authenticated", "own": True} for op in ("read", "create", "update", "delete")}
    resource = make_collection(
        client,
        base,
        headers,
        fields={
            "title": {"type": "string", "required": True},
            "status": {"type": "string", "default": "draft", "indexed": True, "write": {"audience": "owner"}},
        },
        permissions=permissions,
    )
    owner_row = records(
        client, base, headers, action="create", data={"title": "Owner private"}, idempotency_key=str(uuid.uuid4())
    ).json()
    response = records(
        client,
        base,
        member_headers,
        action="create",
        data={"title": "Member private"},
        idempotency_key=str(uuid.uuid4()),
    )
    assert response.status_code == 200, response.text
    member_row = response.json()
    assert records(client, base, headers, action="get", id=member_row["id"]).status_code == 404
    assert records(client, base, member_headers, action="get", id=owner_row["id"]).status_code == 404
    response = records(
        client,
        base,
        member_headers,
        action="update",
        id=member_row["id"],
        data={"status": "published"},
        expected_revision=1,
        idempotency_key=str(uuid.uuid4()),
    )
    assert response.status_code == 403, response.text
    assert (
        client.post(
            base + "/resources",
            headers=member_headers,
            json={
                "action": "delete",
                "resource": resource["id"],
                "expected_revision": 1,
                "idempotency_key": str(uuid.uuid4()),
            },
        ).status_code
        == 403
    )
    assert client.get(base + "/analytics", headers=member_headers).status_code == 403
    definition = client.get(base + "/resources", headers=headers).json()["items"][0]
    definition.pop("id")
    definition.pop("revision")
    definition["permissions"]["update"] = {"audience": "none"}
    assert (
        client.post(
            base + "/resources",
            headers=headers,
            json={
                "action": "update",
                "resource": resource["id"],
                "definition": definition,
                "expected_revision": 1,
                "idempotency_key": str(uuid.uuid4()),
            },
        ).status_code
        == 200
    )
    assert (
        records(
            client,
            base,
            member_headers,
            action="update",
            id=member_row["id"],
            data={"title": "Stale session"},
            expected_revision=1,
            idempotency_key=str(uuid.uuid4()),
        ).status_code
        == 403
    )


@pytest.mark.e2e
def test_group_draft_access_and_public_projection_are_enforced_per_row(
    artifact_api, invite_user_to_org, create_group, add_user_to_group, enterprise_license
):
    client, base, headers, report_id = artifact_api
    org = headers["X-Organization-Id"]
    admin_token = headers["Authorization"].split()[1]
    member = invite_user_to_org(org_id=org, admin_token=admin_token)
    group = create_group(name="Content team", org_id=org, user_token=admin_token)
    assert group.status_code == 200, group.text
    group_id = group.json()["id"]
    assert add_user_to_group(
        group_id=group_id, user_id=member["user_id"], user_token=admin_token, org_id=org
    ).status_code in (200, 201)
    member_headers = {**headers, "Authorization": "Bearer " + member["token"]}
    make_collection(
        client,
        base,
        headers,
        permissions={
            "read": {
                "any_of": [
                    {"audience": "public", "equals": {"status": "published"}, "fields": ["title", "status"]},
                    {"audience": "groups", "group_ids": [group_id]},
                ]
            }
        },
    )
    draft = records(
        client,
        base,
        headers,
        action="create",
        data={"title": "Draft", "slug": "secret-draft"},
        idempotency_key=str(uuid.uuid4()),
    ).json()
    published = records(
        client,
        base,
        headers,
        action="create",
        data={"title": "Published", "status": "published", "slug": "visible-to-team"},
        idempotency_key=str(uuid.uuid4()),
    ).json()
    assert (
        client.put(
            f"/api/reports/{report_id}/visibility/artifact", headers=headers, json={"visibility": "public"}
        ).status_code
        == 200
    )
    team = records(client, base, member_headers, action="list")
    assert team.status_code == 200, team.text
    assert {r["id"] for r in team.json()["items"]} == {draft["id"], published["id"]}
    assert all("slug" in r["data"] for r in team.json()["items"])
    public = records(client, base, {}, action="list").json()
    assert [r["id"] for r in public["items"]] == [published["id"]]
    assert "slug" not in public["items"][0]["data"]
    removal = client.delete(f"/api/organizations/{org}/groups/{group_id}/members/{member['user_id']}", headers=headers)
    assert removal.status_code in (200, 204), removal.text
    assert records(client, base, member_headers, action="get", id=draft["id"]).status_code == 404
    assert "slug" not in records(client, base, member_headers, action="get", id=published["id"]).json()["data"]


@pytest.mark.e2e
@pytest.mark.parametrize("provider_failure", ["success", "failure", "revoked", "file_revoked"])
def test_streaming_uses_authorized_operation_and_preserves_text(
    artifact_api, monkeypatch, create_llm_provider_and_models, provider_failure, caplog, tmp_path
):
    import json
    from app.ai.llm.clients.openai_client import OpenAi
    from app.ai.llm.clients.openai_responses_client import OpenAIResponsesClient

    import logging

    provider_logger = logging.getLogger("openai._base_client")
    monkeypatch.setattr(provider_logger, "disabled", False)
    monkeypatch.setattr(provider_logger, "handlers", [caplog.handler])
    caplog.set_level(logging.WARNING, logger=provider_logger.name)
    provider_logger.warning("Capture is active")
    assert "Capture is active" in caplog.text
    caplog.clear()
    client, base, headers, report_id = artifact_api
    if provider_failure in ("revoked", "file_revoked"):
        import itertools, time
        from types import SimpleNamespace
        from app.routes import artifact_resources

        ticks = itertools.count(0, 11)
        monkeypatch.setattr(artifact_resources, "time", SimpleNamespace(monotonic=lambda: next(ticks), time=time.time))
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    create_llm_provider_and_models(user_token=headers["Authorization"].split()[1], org_id=headers["X-Organization-Id"])
    models = client.get("/api/llm/models", headers=headers).json()
    model = next(m for m in models if m["model_id"] == "gpt-6-luna")
    file_id = None
    if provider_failure == "file_revoked":
        monkeypatch.setenv("BOW_ARTIFACT_STORAGE", str(tmp_path))
        declared = client.post(
            base + "/resources",
            headers=headers,
            json={
                "action": "create",
                "idempotency_key": str(uuid.uuid4()),
                "definition": {"name": "documents", "kind": "files"},
            },
        )
        assert declared.status_code == 200
        uploaded = client.post(
            base + "/files/documents/upload",
            headers=headers,
            files={"file": ("note.txt", b"Synthetic attachment.", "text/plain")},
        )
        assert uploaded.status_code == 200
        file_id = uploaded.json()["id"]

    async def controlled_provider(self, model_id, prompt, images=None, *, max_output_tokens=None):
        import logging

        assert max_output_tokens == 4096
        logger = logging.getLogger("openai._base_client")
        logger.warning("private-artifact-test-marker before token")
        yield "A summary with "
        logger.warning("private-artifact-test-marker after token")
        if provider_failure == "failure":
            raise RuntimeError("Provider unavailable")
        if provider_failure == "revoked":
            revoked = await asyncio.to_thread(client.delete, f"/api/reports/{report_id}", headers=headers)
            assert revoked.status_code in (200, 204), revoked.text
        if provider_failure == "file_revoked":
            revoked = await asyncio.to_thread(
                client.post,
                base + "/resources",
                headers=headers,
                json={
                    "action": "update",
                    "resource": "documents",
                    "expected_revision": 1,
                    "idempotency_key": str(uuid.uuid4()),
                    "definition": {"name": "documents", "kind": "files", "permissions": {"read": {"audience": "none"}}},
                },
            )
            assert revoked.status_code == 200, revoked.text
        yield "```code``` and [brackets]."

    # Only the external provider boundary is controlled. Auth, policies,
    # LLM wrapper, streaming, accounting and storage run normally.
    monkeypatch.setattr(OpenAi, "inference_stream", controlled_provider)
    monkeypatch.setattr(OpenAIResponsesClient, "inference_stream", controlled_provider)
    response = client.post(
        base + "/resources",
        headers=headers,
        json={
            "action": "create",
            "idempotency_key": str(uuid.uuid4()),
            "definition": {
                "name": "summarize",
                "kind": "ai",
                **({"model_id": model["id"]} if provider_failure != "success" else {}),
                "prompt": "Summarize the supplied text.",
                **({"file_resource": "documents"} if file_id else {}),
            },
        },
    )
    assert response.status_code == 200, response.text
    response = client.post(
        base + "/ai/summarize/stream",
        headers=headers,
        json={"text": "Synthetic document input.", **({"fileId": file_id} if file_id else {})},
    )
    assert response.status_code == 200, response.text
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[0]["type"] == "text_delta"
    assert "private-artifact-test-marker" not in caplog.text
    if provider_failure != "success":
        assert events[-1]["type"] == "error"
        assert not any(e["type"] == "completed" for e in events)
    else:
        assert events[-1]["type"] == "completed"
        assert events[-1]["output"] == "A summary with ```code``` and [brackets]."
    assert client.post(base + "/ai/summarize/stream", json={"text": "No identity"}).status_code in (403, 404)
    # There is no execution-history API to recover or resume a paid call.
    assert client.get(base + "/runs", headers=headers).status_code == 404


@pytest.mark.e2e
def test_concurrent_writers_cannot_exceed_quota_or_duplicate_retry(artifact_api):
    from concurrent.futures import ThreadPoolExecutor

    client, base, headers, _ = artifact_api
    make_collection(client, base, headers, max_records=2)

    def write(number):
        return records(
            client, base, headers, action="create", data={"title": str(number)}, idempotency_key=str(uuid.uuid4())
        ).status_code

    with ThreadPoolExecutor(max_workers=6) as executor:
        statuses = list(executor.map(write, range(6)))
    assert statuses.count(200) == 2, statuses
    assert statuses.count(429) == 4, statuses
    assert len(records(client, base, headers, action="list").json()["items"]) == 2


@pytest.mark.e2e
def test_admission_limit_is_shared_across_database_sessions(artifact_api):
    from app.services.artifact_admission import admit
    from app.errors import AppError

    scope = str(uuid.uuid4())

    async def run():
        async def attempt():
            async with async_session_maker() as db:
                try:
                    await admit(db, scope, 3, now=120)
                    return True
                except AppError as exc:
                    assert exc.status_code == 429
                    return False

        return await asyncio.gather(*(attempt() for _ in range(8)))

    outcomes = asyncio.run(run())
    assert sum(outcomes) == 3


@pytest.mark.e2e
def test_archiving_the_report_suspends_its_resource_runtime(artifact_api):
    client, base, headers, report_id = artifact_api
    make_collection(client, base, headers)
    assert (
        records(
            client,
            base,
            headers,
            action="create",
            data={"title": "Retained when archived"},
            idempotency_key=str(uuid.uuid4()),
        ).status_code
        == 200
    )
    response = client.delete("/api/reports/" + report_id, headers=headers)
    assert response.status_code == 200, response.text
    assert records(client, base, headers, action="list").status_code == 404
    assert client.get(base + "/view-token?surface=embedded", headers=headers).status_code == 404


@pytest.mark.e2e
def test_public_file_delivery_requires_a_current_readable_reference(
    artifact_api, monkeypatch, tmp_path, invite_user_to_org
):
    client, base, headers, report_id = artifact_api
    monkeypatch.setenv("BOW_ARTIFACT_STORAGE", str(tmp_path))
    member = invite_user_to_org(org_id=headers["X-Organization-Id"], admin_token=headers["Authorization"].split()[1])
    member_headers = {**headers, "Authorization": "Bearer " + member["token"]}
    response = client.post(
        base + "/resources",
        headers=headers,
        json={
            "action": "create",
            "idempotency_key": str(uuid.uuid4()),
            "definition": {
                "name": "covers",
                "kind": "files",
                "permissions": {"read": {"any_of": [{"audience": "owner"}, {"audience": "public"}]}},
            },
        },
    )
    assert response.status_code == 200, response.text
    response = client.post(
        base + "/files/covers/upload",
        headers=headers,
        files={"file": ("cover.txt", b"Synthetic public attachment", "text/plain")},
    )
    assert response.status_code == 200, response.text
    file = response.json()
    make_collection(
        client,
        base,
        headers,
        fields={
            "title": {"type": "string"},
            "status": {"type": "string", "default": "draft", "indexed": True},
            "cover": {"type": "file"},
        },
        permissions={
            "read": {"any_of": [{"audience": "owner"}, {"audience": "public", "equals": {"status": "published"}}]}
        },
    )
    # Private references beyond the ordinary page size must not hide a later public reference.
    for i in range(101):
        assert (
            records(
                client,
                base,
                headers,
                action="create",
                data={"title": f"Draft {i}", "cover": file["id"]},
                idempotency_key=str(uuid.uuid4()),
            ).status_code
            == 200
        )
    row = records(
        client,
        base,
        headers,
        action="create",
        data={"title": "Preview", "cover": file["id"]},
        idempotency_key=str(uuid.uuid4()),
    ).json()
    assert (
        client.put(
            f"/api/reports/{report_id}/visibility/artifact", headers=headers, json={"visibility": "public"}
        ).status_code
        == 200
    )
    for viewer in ({}, member_headers):
        assert client.get(base + "/files/" + file["id"] + "/content", headers=viewer).status_code == 404
    assert (
        records(
            client,
            base,
            headers,
            action="update",
            id=row["id"],
            data={"status": "published"},
            expected_revision=1,
            idempotency_key=str(uuid.uuid4()),
        ).status_code
        == 200
    )
    assert client.get(base + "/files/" + file["id"] + "/content").content == b"Synthetic public attachment"
    assert client.get("/api/files/" + file["id"] + "/content", headers=headers).status_code == 404
    assert (
        records(
            client,
            base,
            headers,
            action="update",
            id=row["id"],
            data={"status": "draft"},
            expected_revision=2,
            idempotency_key=str(uuid.uuid4()),
        ).status_code
        == 200
    )
    for viewer in ({}, member_headers):
        assert client.get(base + "/files/" + file["id"] + "/content", headers=viewer).status_code == 404


@pytest.mark.e2e
def test_storage_budget_spans_artifacts_and_releases_on_delete(artifact_api, monkeypatch, create_report):
    client, base, headers, _ = artifact_api
    monkeypatch.setenv("BOW_ARTIFACT_ORG_MAX_BYTES", "128")
    make_collection(client, base, headers, fields={"title": {"type": "string"}})
    report = create_report(user_token=headers["Authorization"].split()[1], org_id=headers["X-Organization-Id"])
    response = client.post(
        "/api/artifacts",
        headers=headers,
        json={
            "report_id": report["id"],
            "title": "Second collection",
            "content": {"code": "<div>Budget test</div>", "visualization_ids": []},
        },
    )
    assert response.status_code == 200, response.text
    second = "/api/artifacts/" + response.json()["artifact_id"] + "/runtime"
    make_collection(client, second, headers, fields={"title": {"type": "string"}})
    first = records(client, base, headers, action="create", data={"title": "x" * 70}, idempotency_key=str(uuid.uuid4()))
    assert first.status_code == 200, first.text
    assert (
        records(
            client, second, headers, action="create", data={"title": "y" * 70}, idempotency_key=str(uuid.uuid4())
        ).status_code
        == 429
    )
    row = first.json()
    assert (
        records(
            client,
            base,
            headers,
            action="delete",
            id=row["id"],
            expected_revision=row["revision"],
            idempotency_key=str(uuid.uuid4()),
        ).status_code
        == 200
    )
    assert (
        records(
            client, second, headers, action="create", data={"title": "y" * 70}, idempotency_key=str(uuid.uuid4())
        ).status_code
        == 200
    )


@pytest.mark.e2e
def test_mcp_resource_authoring_uses_same_scope_revisions_and_permissions(
    artifact_api, invite_user_to_org, monkeypatch
):
    from app.ai.tools.mcp import get_mcp_tool, list_mcp_tools
    from app.models.user import User
    from app.models.organization import Organization
    from app.errors import AppError

    client, base, headers, report_id = artifact_api
    member = invite_user_to_org(org_id=headers["X-Organization-Id"], admin_token=headers["Authorization"].split()[1])
    parent = base.split("/")[3]

    async def call(payload, as_member=False):
        async with async_session_maker() as db:
            report = await db.get(Report, report_id)
            user = await db.get(User, member["user_id"] if as_member else report.user_id)
            org = await db.get(Organization, report.organization_id)
            return await get_mcp_tool("manage_artifact_resources")().execute(
                {"report_id": report_id, "artifact_id": parent, **payload}, db, user, org
            )

    assert "manage_artifact_resources" in {t["name"] for t in list_mcp_tools()}
    created = asyncio.run(
        call(
            {
                "action": "create",
                "idempotency_key": str(uuid.uuid4()),
                "definition": {"name": "entries", "fields": {"title": {"type": "string"}}},
            }
        )
    )
    assert created["revision"] == 1
    assert asyncio.run(call({"action": "read"}))["resources"][0]["name"] == "entries"
    assert (
        records(
            client, base, headers, action="create", data={"title": "Kept"}, idempotency_key=str(uuid.uuid4())
        ).status_code
        == 200
    )
    update = {
        "action": "update",
        "resource": "entries",
        "expected_revision": 1,
        "idempotency_key": str(uuid.uuid4()),
        "definition": {"name": "entries", "fields": {"title": {"type": "string"}, "note": {"type": "string"}}},
    }
    assert asyncio.run(call(update))["revision"] == 2
    with pytest.raises(AppError):
        asyncio.run(call({**update, "idempotency_key": str(uuid.uuid4())}))
    from fastapi import HTTPException

    with pytest.raises((AppError, HTTPException)) as denied:
        asyncio.run(call(update, as_member=True))
    assert denied.value.status_code in (403, 404)

    async def reject_nonowner_authoring():
        async with async_session_maker() as db:
            report = await db.get(Report, report_id)
            user = await db.get(User, member["user_id"])
            org = await db.get(Organization, report.organization_id)
            for name in ("create_artifact", "edit_artifact"):
                result = await get_mcp_tool(name)().execute(
                    {
                        "report_id": report_id,
                        "artifact_id": parent,
                        "prompt": "A small app",
                        "edit_instruction": "Change the title",
                    },
                    db,
                    user,
                    org,
                )
                assert result["success"] is False
                assert result["error_message"]
                assert not result.get("artifact_id")

    asyncio.run(reject_nonowner_authoring())
    assert records(client, base, headers, action="list").json()["items"][0]["data"]["title"] == "Kept"
    response = client.put("/api/organization/settings", headers=headers, json={"config": {"enable_artifact_resources": {"value": False}}})
    assert response.status_code == 200
    assert "manage_artifact_resources" in {t["name"] for t in list_mcp_tools()}
    with pytest.raises(AppError):
        asyncio.run(call({"action": "read"}))

@pytest.mark.e2e
def test_deleted_resources_release_live_capacity_without_rebinding_names(artifact_api):
    client, base, headers, _ = artifact_api
    names=[]
    # Exceed the original lifetime limit through the public configuration API.
    for i in range(51):
        name=f'collection_{i}'
        made=make_collection(client,base,headers,name=name)
        names.append(name)
        response=client.post(base+'/resources',headers=headers,json={
            'action':'delete','resource':name,'expected_revision':made['revision'],
            'idempotency_key':str(uuid.uuid4()),
        })
        assert response.status_code==200,response.text
    assert client.get(base+'/resources',headers=headers).json()['items']==[]
    reused=client.post(base+'/resources',headers=headers,json={
        'action':'create','definition':{'name':names[0],'fields':{'title':{'type':'string'}}},
        'idempotency_key':str(uuid.uuid4()),
    })
    assert reused.status_code==409
    make_collection(client,base,headers,name='current_collection')


@pytest.mark.e2e
def test_org_resource_switch_explicit_opt_in_preserves_data(artifact_api, monkeypatch):
    client, base, headers, _ = artifact_api
    monkeypatch.delenv('BOW_ARTIFACT_RESOURCES_ENABLED', raising=False)
    settings = client.get('/api/organization/settings', headers=headers)
    assert settings.status_code == 200
    assert settings.json()['config']['enable_artifact_resources']['value'] is True
    make_collection(client, base, headers)
    assert records(client, base, headers, action='create', data={'title': 'Retained'},
                   idempotency_key=str(uuid.uuid4())).status_code == 200
    for enabled in (False, True):
        changed = client.put('/api/organization/settings', headers=headers, json={
            'config': {'enable_artifact_resources': {'value': enabled}}})
        assert changed.status_code == 200, changed.text
        response = client.get(base + '/resources', headers=headers)
        assert response.status_code == (200 if enabled else 404), response.text
    assert records(client, base, headers, action='list').json()['items'][0]['data']['title'] == 'Retained'


@pytest.mark.e2e
def test_resource_setting_is_admin_only_and_tenant_scoped(artifact_api, invite_user_to_org, create_organization, monkeypatch):
    client, base, headers, report_id = artifact_api
    member = invite_user_to_org(org_id=headers['X-Organization-Id'], admin_token=headers['Authorization'].split()[1])
    member_headers = {**headers, 'Authorization': 'Bearer ' + member['token']}
    payload = {'config': {'enable_artifact_resources': {'value': False}}}
    assert client.put('/api/organization/settings', headers=member_headers, json=payload).status_code == 403
    assert client.get(base + '/resources', headers=headers).status_code == 200
    from app.settings.config import settings
    monkeypatch.setattr(settings.bow_config.features, 'allow_multiple_organizations', True)
    other_org = create_organization(user_token=headers['Authorization'].split()[1])
    assert client.put('/api/organization/settings', headers={**headers, 'X-Organization-Id': other_org},
                      json={'config': {'enable_artifact_resources': {'value': True}}}).status_code == 200
    assert client.put('/api/organization/settings', headers=headers, json=payload).status_code == 200

    async def verify():
        from app.services.artifact_resource_policy import artifact_resources_enabled
        from app.ai.tools.implementations.create_artifact import CreateArtifactTool
        from app.models.organization import Organization
        from app.models.user import User
        async with async_session_maker() as db:
            assert await artifact_resources_enabled(db, other_org)
            assert not await artifact_resources_enabled(db, headers['X-Organization-Id'])
            report = await db.get(Report, report_id)
            ctx = {'db': db, 'report': report, 'organization': await db.get(Organization, report.organization_id),
                   'user': await db.get(User, report.user_id)}
            # No model/provider is supplied: disabled resource creation must exit
            # before generation or any pending artifact/version is created.
            events = [e async for e in CreateArtifactTool().run_stream(
                {'prompt': 'a blog', 'resources': [{'name': 'posts', 'fields': {'title': {'type': 'string'}}}]}, ctx)]
            assert events[-1].type == 'tool.end'
            assert events[-1].payload['output']['success'] is False
            assert not any(e.payload.get('stage') == 'artifact_created' for e in events)
    asyncio.run(verify())
    # The retired process flag must not override organization policy.
    monkeypatch.setenv('BOW_ARTIFACT_RESOURCES_ENABLED', 'false')
    assert client.put('/api/organization/settings', headers=headers,
                      json={'config': {'enable_artifact_resources': {'value': True}}}).status_code == 200
    assert client.get(base + '/resources', headers=headers).status_code == 200


@pytest.mark.e2e
def test_resource_changes_describe_committed_schema_and_permissions(artifact_api):
    client, base, headers, _ = artifact_api
    created = make_collection(client, base, headers)
    assert created['action'] == 'create' and created['committed'] is True
    assert created['name'] == 'entries' and created['definition']['fields']['title']['required']
    definition = created['definition']
    definition['permissions']['read'] = {'audience': 'public'}
    response = client.post(base+'/resources', headers=headers, json={
        'action':'update', 'resource':'entries','definition':definition,
        'expected_revision':created['revision'],'idempotency_key':str(uuid.uuid4())})
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated['resource_artifact_id'] == base.split('/')[3]
    assert updated['revision'] > created['revision'] and updated['records_preserved'] is True
    assert updated['changes']['permissions']['after']['read']['audience'] == 'public'
    assert updated['changes']['permissions']['before']['read']['audience'] != 'public'
    assert 'records' not in updated and 'files' not in updated

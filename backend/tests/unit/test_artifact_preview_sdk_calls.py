"""BOW's own preview reports what the app did through bow.* (services/
artifact_preview_service.py): every call is evidence, a refused call fails
the check the agent is waiting on, and the real conversation policy (asked by
the artifact's own id) is the only runtime read the preview forwards."""
from types import SimpleNamespace

import pytest

from app.services.artifact_preview_service import ArtifactPreviewService


def _service():
    service = ArtifactPreviewService({"report": SimpleNamespace(id="report-1"),
                                      "organization": SimpleNamespace(id="org-1"),
                                      "user": SimpleNamespace(id="user-1")})
    service.artifact = {"id": "version-2", "artifact_id": "artifact-1", "version": 2}
    service.origin = "https://preview.test"
    return service


class _Route:
    def __init__(self, path, method="GET"):
        self.request = SimpleNamespace(url="https://preview.test" + path, method=method, post_data_json=None,
                                       is_navigation_request=lambda: False)
        self.response = None

    async def fulfill(self, *, status=200, json):
        self.response = {"status": status, "json": json}


class _Response:
    status_code = 200

    def json(self):
        return {"available": True, "agents_mode": "auto", "agents": [{"id": "ds-1", "name": "Music Store"}]}


class _Client:
    def __init__(self):
        self.paths = []

    async def get(self, path):
        self.paths.append(path)
        return _Response()


@pytest.mark.asyncio
async def test_a_refused_sdk_call_fails_the_check_and_is_reported():
    service = _service()
    action = service.begin_action()
    service.record("sdk_call", call="request", op="conversations.list", status="ok")
    service.record("sdk_call", call="request", op="conversations.create", status="error",
                   error_code="conversation.agent_forbidden", detail={"agents": ["Music Store"]})
    assert await service.wait(action, timeout=0) == "failed"
    evidence = service.evidence(since=0)
    assert [c["op"] for c in evidence["sdk_calls"]] == ["conversations.list", "conversations.create"]
    assert any(e.get("error_code") == "conversation.agent_forbidden" for e in evidence["errors"])


@pytest.mark.asyncio
async def test_successful_sdk_calls_are_observed_without_a_query():
    service = _service()
    action = service.begin_action()
    service.record("sdk_call", call="stream", op="conversations.send", status="completed")
    assert await service.wait(action, timeout=1) == "sdk_calls_observed"
    assert service.evidence(since=0)["errors"] == []


@pytest.mark.asyncio
async def test_only_the_artifact_s_own_conversation_policy_is_forwarded():
    service = _service()
    service.client = _Client()
    allowed = _Route("/api/artifacts/artifact-1/runtime/conversations/policy")
    await service.route(allowed)
    assert allowed.response["status"] == 200 and allowed.response["json"]["agents"][0]["id"] == "ds-1"
    for path in ("/api/artifacts/other-artifact/runtime/conversations/policy",
                 "/api/artifacts/artifact-1/runtime/conversations",
                 "/api/artifacts/artifact-1/runtime/collections/notes/records"):
        refused = _Route(path)
        await service.route(refused)
        assert refused.response["status"] == 403, path
    assert service.client.paths == ["/api/artifacts/artifact-1/runtime/conversations/policy"]

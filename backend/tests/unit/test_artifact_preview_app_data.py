"""The artifact preview broker lets a previewed app READ its own app data and
blocks every app-data write (the preview is read-only)."""

from types import SimpleNamespace

import pytest

from app.services.artifact_preview_service import ArtifactPreviewService

ORIGIN = "https://preview.test"
PARENT_ID = "parent-artifact-1"


class _Response:
    def __init__(self, payload, status_code):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class _Client:
    """Replaces only the server-side HTTP boundary; records every dispatch."""

    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.calls = []

    async def get(self, path, **kwargs):
        self.calls.append(("GET", path))
        return _Response(self.payload, self.status_code)

    async def post(self, path, **kwargs):
        self.calls.append(("POST", path))
        return _Response(self.payload, self.status_code)


class _Route:
    def __init__(self, method, path, body=None):
        self.request = SimpleNamespace(url=ORIGIN + path, method=method, post_data_json=body)
        self.response = None

    async def fulfill(self, *, status=200, json):
        self.response = {"status": status, "json": json}
        return self.response


def _service(client):
    service = ArtifactPreviewService({
        "report": SimpleNamespace(id="report-1"),
        "organization": SimpleNamespace(id="org-1"),
        "user": SimpleNamespace(id="user-1"),
    })
    # `id` is the previewed VERSION; data belongs to the PARENT artifact id.
    service.artifact = {"id": "version-3", "artifact_id": PARENT_ID}
    service.origin = ORIGIN
    service.client = client
    return service


@pytest.mark.asyncio
async def test_get_own_app_data_is_proxied_and_recorded():
    items = {"items": [{"id": "r1", "data": {"text": "hi"}, "version": 1}]}
    client = _Client(items)
    service = _service(client)
    route = _Route("GET", f"/api/artifacts/{PARENT_ID}/data/notes")

    await service.route(route)

    assert client.calls == [("GET", f"/api/artifacts/{PARENT_ID}/data/notes")]
    assert route.response == {"status": 200, "json": items}
    events = [e for e in service.events if e["kind"] == "app_data"]
    assert len(events) == 1
    assert events[0]["collection"] == "notes"
    assert events[0]["http_status"] == 200


@pytest.mark.asyncio
async def test_get_app_data_passes_server_denials_through():
    body = {"detail": "Not allowed", "error_code": "app_data.forbidden", "status_code": 403}
    client = _Client(body, status_code=403)
    service = _service(client)
    route = _Route("GET", f"/api/artifacts/{PARENT_ID}/data/secrets")

    await service.route(route)

    assert route.response == {"status": 403, "json": body}
    event = [e for e in service.events if e["kind"] == "app_data"][-1]
    assert (event["collection"], event["http_status"]) == ("secrets", 403)


@pytest.mark.parametrize(("method", "path", "body"), [
    ("POST", f"/api/artifacts/{PARENT_ID}/data/notes", {"data": {"text": "x"}}),
    ("PATCH", f"/api/artifacts/{PARENT_ID}/data/notes/r1", {"data": {"text": "x"}, "version": 1}),
    ("DELETE", f"/api/artifacts/{PARENT_ID}/data/notes/r1", {"version": 1}),
])
@pytest.mark.asyncio
async def test_app_data_writes_are_blocked_before_dispatch(method, path, body):
    client = _Client({"id": "r1"})
    service = _service(client)
    route = _Route(method, path, body)

    await service.route(route)

    assert client.calls == []
    assert route.response["status"] == 403
    assert service.events[-1]["kind"] == "blocked"
    assert not [e for e in service.events if e["kind"] == "app_data"]


@pytest.mark.parametrize("path", [
    "/api/artifacts/other-artifact/data/notes",   # foreign artifact id
    "/api/artifacts/version-3/data/notes",        # the VERSION id is not the data key
    f"/api/artifacts/{PARENT_ID}/data/Notes",     # not a valid collection name
    f"/api/artifacts/{PARENT_ID}/data/notes/r1",  # single-record GET is not an endpoint
    f"/api/artifacts/{PARENT_ID}x/data/notes",    # id prefix match is not enough
])
@pytest.mark.asyncio
async def test_out_of_scope_app_data_reads_are_blocked(path):
    client = _Client({"items": []})
    service = _service(client)
    route = _Route("GET", path)

    await service.route(route)

    assert client.calls == []
    assert route.response["status"] == 403
    assert service.events[-1]["kind"] == "blocked"


@pytest.mark.asyncio
async def test_foreign_origin_is_still_outside_preview_scope():
    client = _Client({"items": []})
    service = _service(client)
    route = _Route("GET", f"/api/artifacts/{PARENT_ID}/data/notes")
    route.request.url = "https://elsewhere.test" + f"/api/artifacts/{PARENT_ID}/data/notes"
    route.aborted = False

    async def abort():
        route.aborted = True
    route.abort = abort

    await service.route(route)

    assert route.aborted is True
    assert client.calls == []

"""No stack traces / internal paths in API error responses (pentest 2026-10, group A).

Contract under test:
* Malformed input never produces a 500 with exception text: control
  characters in the URL are a 400, a non-UUID value for a UUID path id is the
  same 404 an unknown id gets, malformed bodies are a 4xx.
* Anything that still escapes as an unhandled exception is a generic JSON 500
  carrying only ``detail`` + ``request_id`` (no traceback, no file paths).
* Starlette's HTML traceback page stays off even on a DEBUG deployment unless
  ``DEBUG_TRACEBACKS`` is explicitly enabled.

Each case is tagged with the pentest finding id it reproduces.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from main import app

LEAK_MARKERS = ("Traceback", 'File "', ".py", "/home/", "site-packages", "sqlalchemy", "Exception")


def _uid() -> str:
    return str(uuid.uuid4())


@pytest.fixture(scope="module")
def client():
    # raise_server_exceptions=False: observe what a real HTTP client receives
    # instead of having the TestClient re-raise server-side exceptions.
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def admin(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return {"token": token, "org_id": org_id,
            "headers": {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}}


def _assert_no_leak(resp):
    body = resp.text
    for marker in LEAK_MARKERS:
        assert marker not in body, f"{resp.status_code} body leaks {marker!r}: {body[:300]}"
    if resp.status_code >= 500:
        data = resp.json()
        assert data.get("detail") == "Internal server error"
        assert data.get("request_id")


NUL = "%00"

# (finding, method, path, kwargs) — every request carries a NUL / control byte
# in the URL; all must be rejected before routing with a 400.
CONTROL_CHAR_CASES = [
    ("828529622", "post", f"/api/queries/{NUL}/preview", {"json": {}}),
    ("828608699", "get", f"/index{NUL}.html", {}),
    ("828741193", "post", f"/api/instructions/x{NUL}/versions/{_uid()}/revert", {}),
    ("828744245", "get", f"/api/prompts/x{NUL}/run-for/targets", {}),
    ("828814496", "get", f"/api/reports/x{NUL}", {}),
    ("828816455", "get", f"/api/tests/suites/x{NUL}/export", {}),
    ("828838472", "post", f"/api/instructions/{_uid()}/versions/x{NUL}/revert", {}),
    ("828868435", "get", f"/api/settings/integrations/x{NUL}", {}),
    ("828903078", "get", "/api/organizations/{org}/role-assignments?principal_id=a" + NUL, {}),
    ("828926448", "post", f"/api/data_sources/{_uid()}/lists/x{NUL}/rows/{_uid()}/revisions/{_uid()}/revert", {}),
    ("829000740", "post", f"/api/llm/models/x{NUL}/toggle?enabled=true", {}),
    ("829378511", "get", f"/api/data_sources/x{NUL}/files", {}),
    ("829382648", "get", f"/api/console/trace/x{NUL}/{_uid()}", {}),
    ("829394253", "get", "/api/console/metrics?data_source_ids=a" + NUL, {}),
    ("829403280", "put", f"/api/connections/{_uid()}/custom-queries/x{NUL}", {"json": {}}),
    ("829403680", "get", f"/api/artifacts/x{NUL}/runtime/view-token", {}),
    ("829405483", "get", f"/api/r/{_uid()}/chat/completions/x{NUL}/stream", {}),
    ("829413564", "delete", f"/api/connections/x{NUL}/my-credentials", {}),
    ("control-newline", "get", "/api/reports/a%0Ab", {}),
]


@pytest.mark.e2e
@pytest.mark.parametrize("finding,method,path,kwargs", CONTROL_CHAR_CASES,
                         ids=[c[0] for c in CONTROL_CHAR_CASES])
@pytest.mark.parametrize("authenticated", [True, False], ids=["authed", "anon"])
def test_control_characters_in_url_are_rejected(client, admin, finding, method, path, kwargs, authenticated):
    headers = admin["headers"] if authenticated else {}
    resp = getattr(client, method)(path.format(org=admin["org_id"]), headers=headers, **kwargs)
    assert resp.status_code == 400, (finding, resp.status_code, resp.text[:300])
    _assert_no_leak(resp)


# Malformed (non-UUID) ids on UUID-keyed routes answer like an unknown id.
MALFORMED_ID_CASES = [
    ("828529622", "post", "/api/queries/{bad}/preview", {"json": {}}),
    ("828744245", "post", "/api/prompts/{bad}/run-for", {"json": {"principal_type": "users", "user_ids": []}}),
    ("828814496", "get", "/api/reports/{bad}", {}),
    ("828816455", "get", "/api/tests/suites/{bad}/export", {}),
    ("829000740", "post", "/api/llm/models/{bad}/toggle?enabled=true", {}),
    ("829403280", "put", "/api/connections/{ok}/custom-queries/{bad}", {"json": {}}),
    ("829403680", "get", "/api/artifacts/{bad}/runtime/view-token", {}),
    ("829405483", "get", "/api/r/{ok}/chat/completions/{bad}/stream", {}),
    ("829413564", "delete", "/api/connections/{bad}/my-credentials", {}),
]


@pytest.mark.e2e
@pytest.mark.parametrize("finding,method,path,kwargs", MALFORMED_ID_CASES,
                         ids=[c[0] for c in MALFORMED_ID_CASES])
@pytest.mark.parametrize("bad", ["not-a-uuid", "1" * 400, "%27%3B--"])
def test_malformed_uuid_path_ids_are_not_found(client, admin, finding, method, path, kwargs, bad):
    resp = getattr(client, method)(path.format(bad=bad, ok=_uid()), headers=admin["headers"], **kwargs)
    assert resp.status_code == 404, (finding, resp.status_code, resp.text[:300])
    _assert_no_leak(resp)


@pytest.mark.e2e
def test_well_formed_unknown_ids_still_404(client, admin):
    """The guard must not change the normal not-found path for real UUIDs."""
    resp = client.get(f"/api/reports/{_uid()}", headers=admin["headers"])
    assert resp.status_code == 404
    _assert_no_leak(resp)


def _body_cases(admin_ctx, client):
    org = admin_ctx["org_id"]
    return [
        # finding, method, path, kwargs, allowed statuses
        ("828511805", "get", f"/api/settings/integrations/{_uid()}/users", {}, {404}),
        ("828729659", "put", "/api/organization/week_start", {"json": {"week_start": 5}}, {422}),
        ("828729659", "put", "/api/organization/week_start", {"json": {"week_start": ["monday"]}}, {422}),
        ("828833572", "post", f"/api/prompts/{_uid()}/run-for",
         {"json": {"principal_type": "users", "user_ids": ["a\u0000b"]}}, {422}),
        ("828833572", "post", f"/api/prompts/{_uid()}/run-for",
         {"json": {"principal_type": "group", "group_id": "x" * 500}}, {422}),
        ("828965020", "post", "/api/api_keys", {"json": {"name": "k" * 5000}}, {422}),
        ("829010358", "post", f"/api/llm/models/{_uid()}/access",
         {"json": {"principal_type": "user", "principal_id": "p" * 5000}}, {422}),
        ("829372210", "post", f"/api/data_sources/{_uid()}/bulk_update_tables",
         {"json": {"action": "activate", "filter": {"schema": 123}}}, {422}),
        ("829372210", "post", f"/api/data_sources/{_uid()}/bulk_update_tables",
         {"json": {"action": "activate", "filter": {"search": {"$ne": 1}}}}, {422}),
        ("829374459", "get", f"/api/data_sources/{_uid()}/generate_items?item=summary", {}, {404}),
        ("829378257", "post", f"/api/data_sources/{_uid()}/my-credentials/test",
         {"json": {"auth_mode": "userpass", "credentials": {}}}, {404}),
        ("829397366", "get", f"/api/console/trace/{_uid()}/{_uid()}", {}, {404}),
        ("829397366", "get", f"/api/console/reports/{_uid()}/conversation", {}, {404}),
        ("829397366", "get", f"/api/console/agent_executions/{_uid()}", {}, {404}),
        ("829416375", "put", "/api/organization/smtp",
         {"json": {"enabled": False, "from_address": "a@b.co", "from_name": "Evil\r\nBcc: x@y.z"}}, {422}),
        ("829416375", "put", "/api/organization/smtp",
         {"json": {"enabled": False, "host": "smtp.example.com\nRCPT TO:<x@y.z>"}}, {422}),
    ]


@pytest.mark.e2e
def test_malformed_bodies_and_unknown_ids_are_4xx(client, admin):
    failures = []
    for finding, method, path, kwargs, allowed in _body_cases(admin, client):
        resp = getattr(client, method)(path, headers=admin["headers"], **kwargs)
        _assert_no_leak(resp)
        if resp.status_code not in allowed:
            failures.append((finding, method, path[:80], resp.status_code, resp.text[:200]))
    assert not failures, failures


@pytest.mark.e2e
def test_valid_week_start_and_smtp_still_accepted(client, admin):
    """Regression: the new type/line-break guards don't reject valid input."""
    assert client.put("/api/organization/week_start", json={"week_start": "monday"},
                      headers=admin["headers"]).status_code == 200
    assert client.put("/api/organization/week_start", json={"week_start": None},
                      headers=admin["headers"]).status_code == 200
    resp = client.put("/api/organization/smtp", headers=admin["headers"], json={
        "enabled": False, "host": "smtp.example.com", "from_address": "a@b.co", "from_name": "Ops Team"})
    assert resp.status_code == 200, resp.text


@pytest.mark.e2e
def test_service_account_and_api_key_names(client, admin):
    """829001162 / 828965020: oversized key names are a 422; normal names work."""
    sa = client.post("/api/service_accounts", json={"name": f"sa-{uuid.uuid4().hex[:6]}"},
                     headers=admin["headers"])
    assert sa.status_code == 200, sa.text
    sa_id = sa.json()["id"]
    too_long = client.post(f"/api/service_accounts/{sa_id}/keys", json={"name": "k" * 5000},
                           headers=admin["headers"])
    assert too_long.status_code == 422
    _assert_no_leak(too_long)
    assert client.post("/api/service_accounts", json={"name": "s" * 5000},
                       headers=admin["headers"]).status_code == 422
    ok = client.post(f"/api/service_accounts/{sa_id}/keys", json={"name": "ci"}, headers=admin["headers"])
    assert ok.status_code == 200, ok.text

    # 828730864: whoami with the service-account key is a normal profile, not a 500.
    who = client.get("/api/users/whoami", headers={"X-API-Key": ok.json()["key"],
                                                   "X-Organization-Id": admin["org_id"]})
    assert who.status_code == 200, who.text
    assert who.json()["email"].endswith("@service.invalid")
    _assert_no_leak(who)


@pytest.mark.e2e
def test_unhandled_exception_is_generic_json_with_request_id(client, admin, monkeypatch):
    """Any bug that escapes as an exception returns a generic 500 body; the
    exception text (which may hold paths/SQL) stays in the server log."""
    from app.routes import user_profile

    async def _boom(*_a, **_k):  # simulates an arbitrary unanticipated server bug
        raise RuntimeError('boom at File "/srv/app/secret.py", line 7')

    monkeypatch.setattr(user_profile.organization_service, "get_user_organizations", _boom)
    resp = client.get("/api/users/whoami", headers={**admin["headers"], "X-Request-ID": "probe-12345678"})
    assert resp.status_code == 500
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["request_id"] == "probe-12345678"
    assert resp.headers.get("x-request-id") == "probe-12345678"
    assert "secret.py" not in resp.text and "boom" not in resp.text
    _assert_no_leak(resp)


@pytest.mark.e2e
def test_typed_and_validation_errors_keep_their_shape(client, admin):
    """HTTPException / RequestValidationError / AppError are not swallowed."""
    # FastAPI validation error: list-shaped detail, 422.
    resp = client.post("/api/api_keys", json={"name": 123}, headers=admin["headers"])
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], list)
    # HTTPException passes through with its own status + detail.
    resp = client.put("/api/organization/week_start", json={"week_start": "someday"}, headers=admin["headers"])
    assert resp.status_code == 422
    assert "week_start" in resp.json()["detail"]
    # Unauthenticated stays 401.
    assert client.get("/api/users/whoami").status_code == 401


def test_traceback_pages_require_explicit_opt_in():
    from app.core.request_hardening import traceback_pages_enabled

    class S:
        def __init__(self, debug, tracebacks):
            self.DEBUG, self.DEBUG_TRACEBACKS = debug, tracebacks

    assert traceback_pages_enabled(S(True, False)) is False
    assert traceback_pages_enabled(S(False, True)) is False
    assert traceback_pages_enabled(S(True, True)) is True
    # The running app honours it (default config never enables tracebacks).
    assert app.debug is False

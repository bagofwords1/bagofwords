"""Database-backed authorization contracts for internal artifact browsing."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User


def _run(coro):
    return asyncio.run(coro)


def _new_owner(
    create_user, login_user, whoami, create_report, title="Browser policy", *,
    create_connection=None, create_domain_from_connection=None, with_browser=False,
):
    email = f"browser-policy-{uuid.uuid4().hex[:10]}@example.com"
    user = create_user(email=email)
    token = login_user(user["email"], user["password"])
    identity = whoami(token)
    org_id = identity["organizations"][0]["id"]
    data_sources = []
    if with_browser:
        connection = create_connection(
            name=f"Browser connection {uuid.uuid4().hex[:8]}",
            type="browser",
            config={"url_patterns": ["https://allowed.example.test/**"], "allow_downloads": False},
            credentials={}, user_token=token, org_id=org_id,
        )
        source = create_domain_from_connection(
            name=f"Browser source {uuid.uuid4().hex[:8]}", connection_id=connection["id"],
            user_token=token, org_id=org_id, is_public=True,
        )
        data_sources.append(source["id"])
    report = create_report(title=title, user_token=token, org_id=org_id, data_sources=data_sources)
    return report, token, org_id, identity["id"]


def _set_browser_settings(update_organization_settings, token, org_id, *, enabled, allow_data=True):
    update_organization_settings(
        {"enable_artifact_verification": enabled, "allow_llm_see_data": allow_data},
        user_token=token,
        org_id=org_id,
    )


def _save_artifact(test_client, report_id, token, org_id, *, mode="page"):
    """Use the artifact API for page and document creation."""
    content = {"code": "export default function App(){ return <main>ok</main> }"}
    if mode == "doc":
        content = {"markdown": "# Policy test document", "visualization_ids": []}
    response = test_client.post(
        "/api/artifacts",
        json={"report_id": report_id, "title": "Policy test artifact", "mode": mode, "content": content},
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)},
    )
    assert response.status_code == 200, response.json()
    return response.json()["id"]


async def _runtime_context(report_id, user_id, org_id):
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        user = await db.get(User, user_id)
        organization = await db.get(Organization, org_id)
        assert report and user and organization
        return {"report": report, "user": user, "organization": organization}


@pytest.mark.e2e
def test_artifact_preview_requires_opt_in_privacy_and_an_exact_saved_page(
    test_client, create_user, login_user, whoami, create_report, update_organization_settings,
):
    report, token, org_id, user_id = _new_owner(create_user, login_user, whoami, create_report)
    artifact_id = _save_artifact(test_client, report["id"], token, org_id)
    ctx = _run(_runtime_context(report["id"], user_id, org_id))
    from app.services.artifact_verification_policy import browser_policy, require_browser_access

    # The opt-in defaults off even when LLM data visibility retains its default.
    initial = _run(browser_policy(ctx, artifact_id))
    assert initial.allow_data is True
    assert initial.artifact_preview is False
    with pytest.raises(PermissionError):
        _run(require_browser_access(ctx, artifact_id))
    _set_browser_settings(update_organization_settings, token, org_id, enabled=True)
    allowed = _run(browser_policy(ctx, artifact_id))
    assert allowed.artifact_preview is True
    _set_browser_settings(update_organization_settings, token, org_id, enabled=True, allow_data=False)
    private = _run(browser_policy(ctx, artifact_id))
    assert private.allow_data is False
    assert private.artifact_preview is False
    with pytest.raises(PermissionError):
        _run(require_browser_access(ctx, artifact_id))


@pytest.mark.e2e
def test_all_browser_tools_fail_closed_before_browser_or_preview_io(
    monkeypatch, test_client, create_user, login_user, whoami, create_report,
):
    report, token, org_id, user_id = _new_owner(create_user, login_user, whoami, create_report)
    artifact_id = _save_artifact(test_client, report["id"], token, org_id)
    ctx = _run(_runtime_context(report["id"], user_id, org_id))
    calls = {"browser": 0, "http": 0}

    import httpx
    import playwright.async_api

    class UnexpectedPlaywrightFactory:
        async def start(self):
            calls["browser"] += 1
            raise AssertionError("policy denial must happen before Playwright starts")

    class UnexpectedHTTPClient:
        def __init__(self, *_args, **_kwargs):
            calls["http"] += 1
            raise AssertionError("policy denial must happen before the preview backend is contacted")

    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: UnexpectedPlaywrightFactory())
    monkeypatch.setattr(httpx, "AsyncClient", UnexpectedHTTPClient)

    async def call_every_tool():
        from app.ai.tools.implementations.browser_act import BrowserActTool
        from app.ai.tools.implementations.browser_extract import BrowserExtractTool
        from app.ai.tools.implementations.browser_navigate import BrowserNavigateTool
        from app.ai.tools.implementations.browser_snapshot import BrowserSnapshotTool
        from app.ai.tools.implementations.browser_vision import BrowserVisionTool

        cases = [
            (BrowserNavigateTool(), {"artifact_id": artifact_id}),
            (BrowserSnapshotTool(), {"session_id": "missing-session"}),
            (BrowserActTool(), {"session_id": "missing-session", "ref": "e1", "action": "click"}),
            (BrowserExtractTool(), {"session_id": "missing-session"}),
            (BrowserVisionTool(), {"session_id": "missing-session"}),
        ]
        results = []
        for tool, tool_input in cases:
            events = [event async for event in tool.run_stream(tool_input, ctx)]
            result = [event.payload for event in events if event.type == "tool.end"][-1]
            results.append(result)
        return results

    results = _run(call_every_tool())
    assert all(result["output"]["success"] is False for result in results)
    assert all(result["output"]["error_code"] == "preview_restricted" for result in results)
    assert all(not result["output"].get(key)
               for result in results
               for key in ("snapshot", "text", "screenshot_file_id", "artifact", "evidence"))
    assert calls == {"browser": 0, "http": 0}


@pytest.mark.e2e
@pytest.mark.parametrize(("status_code", "expected_code"), [(403, "preview_restricted"), (503, "preview_unavailable")])
def test_authorized_preview_startup_errors_never_return_artifact_evidence(
    monkeypatch, status_code, expected_code, test_client, create_user, login_user, whoami,
    create_report, update_organization_settings,
):
    report, token, org_id, user_id = _new_owner(create_user, login_user, whoami, create_report)
    artifact_id = _save_artifact(test_client, report["id"], token, org_id)
    _set_browser_settings(update_organization_settings, token, org_id, enabled=True)

    import app.services.artifact_preview_service as preview_module
    from app.ai.tools.implementations.browser_navigate import BrowserNavigateTool
    from types import SimpleNamespace

    clients = []

    class Response:
        def __init__(self, status):
            self.status_code = status

    class FailingClient:
        def __init__(self, **_kwargs):
            self.closed = False
            clients.append(self)

        async def get(self, _path):
            return Response(status_code)

        async def aclose(self):
            self.closed = True

    async def write_token(_user):
        return "test-token"

    monkeypatch.setattr(preview_module.httpx, "AsyncClient", FailingClient)
    monkeypatch.setattr("app.core.auth.get_jwt_strategy", lambda: SimpleNamespace(write_token=write_token))
    ctx = _run(_runtime_context(report["id"], user_id, org_id))

    async def run_tool():
        return [event async for event in BrowserNavigateTool().run_stream({"artifact_id": artifact_id}, ctx)]

    events = _run(run_tool())
    end = [event for event in events if event.type == "tool.end"][-1].payload
    assert end["output"]["success"] is False
    assert end["output"]["error_code"] == expected_code
    assert all(end["output"].get(key) is None for key in ("snapshot", "evidence", "artifact", "parameters"))
    assert all(end["observation"].get(key) is None for key in ("snapshot", "evidence", "artifact", "parameters"))
    assert clients and clients[0].closed is True

@pytest.mark.e2e
def test_only_saved_live_pages_in_the_current_report_are_catalog_eligible(
    test_client, create_user, login_user, whoami, create_report, update_organization_settings,
    delete_report,
):
    report, token, org_id, user_id = _new_owner(create_user, login_user, whoami, create_report)
    ctx = _run(_runtime_context(report["id"], user_id, org_id))
    _set_browser_settings(update_organization_settings, token, org_id, enabled=True)
    from app.ai.tools.artifact_verification import refresh_browser_tool_catalog
    from app.schemas.ai.planner import ToolDescriptor

    ordinary_tool = ToolDescriptor(name="read_query", description="Unrelated tool")
    browser_candidates = [
        ToolDescriptor(name="browser_navigate", description="Navigate"),
        ToolDescriptor(name="browser_snapshot", description="Snapshot"),
    ]

    def refresh(catalog):
        return _run(refresh_browser_tool_catalog(catalog, browser_candidates, ctx))

    # The same live context transitions from no eligible page, to an eligible
    # saved page, then back to ineligible after deletion. Other catalog filters
    # must survive each refresh.
    catalog = refresh([ordinary_tool, *browser_candidates])
    assert [tool.name for tool in catalog] == ["read_query"]

    live_page = _save_artifact(test_client, report["id"], token, org_id)
    catalog = refresh([ordinary_tool])
    assert [tool.name for tool in catalog] == ["read_query", "browser_navigate", "browser_snapshot"]

    other_report = create_report(title="Other browser policy report", user_token=token, org_id=org_id, data_sources=[])
    other_page = _save_artifact(test_client, other_report["id"], token, org_id)
    doc = _save_artifact(test_client, report["id"], token, org_id, mode="doc")
    from app.services.artifact_verification_policy import browser_policy

    assert _run(browser_policy(ctx)).artifact_preview is True
    delete_response = test_client.delete(
        f"/api/artifacts/{live_page}",
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)},
    )
    assert delete_response.status_code == 200, delete_response.json()
    catalog = _run(browser_policy(ctx))
    assert catalog.artifact_preview is False
    assert [tool.name for tool in refresh([ordinary_tool, *browser_candidates])] == ["read_query"]
    for ineligible_id in (other_page, doc):
        exact = _run(browser_policy(ctx, ineligible_id))
        assert exact.artifact_preview is False
    delete_report(report["id"], user_token=token, org_id=org_id)
    assert _run(browser_policy(ctx, live_page)).artifact_preview is False


@pytest.mark.e2e
def test_report_share_grants_artifact_preview_to_a_member_and_revocation_takes_effect(
    test_client, create_user, login_user, whoami, create_report,
    update_organization_settings, set_visibility,
):
    report, owner_token, org_id, owner_id = _new_owner(create_user, login_user, whoami, create_report)
    artifact_id = _save_artifact(test_client, report["id"], owner_token, org_id)
    member_email = f"browser-member-{uuid.uuid4().hex[:10]}@example.com"
    invite = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers={"Authorization": f"Bearer {owner_token}", "X-Organization-Id": org_id},
    )
    assert invite.status_code == 200, invite.json()
    member = create_user(email=member_email, password="test123")
    member_token = login_user(member["email"], member["password"])
    member_identity = whoami(member_token)
    _set_browser_settings(update_organization_settings, owner_token, org_id, enabled=True)
    set_visibility(report["id"], "artifact", "internal", user_token=owner_token, org_id=org_id)
    member_ctx = _run(_runtime_context(report["id"], member_identity["id"], org_id))
    from app.services.artifact_verification_policy import browser_policy

    allowed = _run(browser_policy(member_ctx, artifact_id))
    assert allowed.artifact_preview is True
    set_visibility(report["id"], "artifact", "none", user_token=owner_token, org_id=org_id)
    revoked = _run(browser_policy(member_ctx, artifact_id))
    assert revoked.artifact_preview is False


@pytest.mark.e2e
def test_attached_authorized_browser_connector_works_while_artifact_verification_is_off(
    create_connection, create_domain_from_connection, create_user, login_user, whoami,
    create_report, update_organization_settings,
):
    report, token, org_id, user_id = _new_owner(
        create_user, login_user, whoami, create_report,
        create_connection=create_connection,
        create_domain_from_connection=create_domain_from_connection,
        with_browser=True,
    )
    ctx = _run(_runtime_context(report["id"], user_id, org_id))
    _set_browser_settings(update_organization_settings, token, org_id, enabled=False)
    from app.services.artifact_verification_policy import browser_policy, require_browser_access

    result = _run(browser_policy(ctx))
    assert result.allow_data is True
    assert result.artifact_preview is False
    assert result.connector is True
    connector_access = _run(require_browser_access(ctx))
    assert connector_access.connector is True

    _set_browser_settings(update_organization_settings, token, org_id, enabled=False, allow_data=False)
    with pytest.raises(PermissionError):
        _run(require_browser_access(ctx))


@pytest.mark.e2e
def test_logout_revokes_a_cached_browser_context(test_client, create_connection,
                                                 create_domain_from_connection,
                                                 create_user, login_user, whoami,
                                                 create_report):
    report, token, org_id, user_id = _new_owner(
        create_user, login_user, whoami, create_report,
        create_connection=create_connection,
        create_domain_from_connection=create_domain_from_connection,
        with_browser=True,
    )
    ctx = _run(_runtime_context(report["id"], user_id, org_id))
    from app.services.artifact_verification_policy import browser_policy, require_browser_access

    assert _run(require_browser_access(ctx)).connector is True
    logout = test_client.post("/api/auth/jwt/logout", headers={"Authorization": f"Bearer {token}"})
    assert logout.status_code in (200, 204), logout.text

    revoked = _run(browser_policy(ctx))
    assert revoked.connector is False
    with pytest.raises(PermissionError):
        _run(require_browser_access(ctx))


@pytest.mark.e2e
def test_revoked_privacy_policy_withholds_navigation_evidence_at_tool_end(
    monkeypatch, test_client, create_connection, create_domain_from_connection, create_user,
    login_user, whoami, create_report, update_organization_settings,
):
    report, token, org_id, user_id = _new_owner(
        create_user, login_user, whoami, create_report,
        create_connection=create_connection,
        create_domain_from_connection=create_domain_from_connection,
        with_browser=True,
    )
    artifact_id = _save_artifact(test_client, report["id"], token, org_id)
    ctx = _run(_runtime_context(report["id"], user_id, org_id))
    _set_browser_settings(update_organization_settings, token, org_id, enabled=True)

    import playwright.async_api
    from app.ai.tools.implementations import browser_navigate
    from app.ai.tools.implementations._browser_common import BrowserSessionManager

    manager = BrowserSessionManager()
    monkeypatch.setattr(browser_navigate, "session_manager", manager)
    monkeypatch.setattr("app.ai.tools.implementations._browser_policy.session_manager", manager)
    revoke_on_goto = {"enabled": False}

    class RevokingPage:
        url = "about:blank"

        def on(self, *_args, **_kwargs):
            pass

        async def goto(self, url, **_kwargs):
            self.url = url
            if revoke_on_goto["enabled"]:
                _set_browser_settings(update_organization_settings, token, org_id, enabled=False, allow_data=False)
            return type("Response", (), {"status": 200})()

        async def title(self):
            return "Confidential dashboard"

        async def evaluate(self, *_args, **_kwargs):
            return []

        def locator(self, _selector):
            return FakeLocator()

    class FakeLocator:
        async def aria_snapshot(self, **_kwargs):
            return '- heading "Confidential dashboard"'

        async def inner_text(self, **_kwargs):
            return "Confidential dashboard"

        async def count(self):
            return 0

    class FakeContext:
        def __init__(self):
            self.page = RevokingPage()

        async def new_page(self):
            return self.page

        async def route(self, *_args, **_kwargs):
            pass

        async def route_web_socket(self, *_args, **_kwargs):
            pass

        async def add_style_tag(self, **_kwargs):
            pass

        def on(self, *_args, **_kwargs):
            pass

        async def close(self):
            pass

    class FakeBrowser:
        async def new_context(self, **_kwargs):
            return FakeContext()

        async def close(self):
            pass

    class FakePlaywright:
        class Chromium:
            async def launch(self, **_kwargs):
                return FakeBrowser()

        chromium = Chromium()

        async def stop(self):
            pass

    class FakeFactory:
        async def start(self):
            return FakePlaywright()

    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: FakeFactory())

    async def run_tool(tool_input):
        from app.ai.tools.implementations.browser_navigate import BrowserNavigateTool
        return [event async for event in BrowserNavigateTool().run_stream(tool_input, ctx)]

    # A stale connector-session ID should recover by opening a fresh session.
    stale_events = _run(run_tool({
        "url": "https://allowed.example.test/dashboard", "session_id": "stale-session-id",
    }))
    stale_output = [event for event in stale_events if event.type == "tool.end"][-1].payload["output"]
    assert stale_output["success"] is True
    assert stale_output["session_id"] != "stale-session-id"

    # An internal preview session is execution-scoped and cannot be reused for
    # external URL navigation, even when the artifact itself is authorized.
    class FakePreview:
        artifact = {"id": artifact_id}

        async def route(self, *_args, **_kwargs):
            pass

        async def close(self):
            pass

    preview_session = _run(manager.open(
        report["id"], ["https://allowed.example.test/**"], False,
        runtime_ctx=ctx, preview=FakePreview(),
    ))
    preview_events = _run(run_tool({
        "url": "https://allowed.example.test/dashboard", "session_id": preview_session.session_id,
    }))
    preview_output = [event for event in preview_events if event.type == "tool.end"][-1].payload["output"]
    assert preview_output["success"] is False
    assert preview_output["error_code"] == "session_scope_mismatch"

    # The organization opt-in alone controls an already-open internal preview;
    # disabling it closes that session while the separately authorized browser
    # connector remains usable.
    _set_browser_settings(update_organization_settings, token, org_id, enabled=False, allow_data=True)
    from app.ai.tools.implementations.browser_snapshot import BrowserSnapshotTool

    revoked_preview_events = _run(_run_snapshot(BrowserSnapshotTool(), preview_session.session_id, ctx))
    revoked_preview_output = [
        event for event in revoked_preview_events if event.type == "tool.end"
    ][-1].payload["output"]
    assert revoked_preview_output["success"] is False
    assert revoked_preview_output["error_code"] == "preview_restricted"
    assert revoked_preview_output.get("snapshot") is None
    assert manager.get(preview_session.session_id, ctx) is None
    from app.services.artifact_verification_policy import require_browser_access
    assert _run(require_browser_access(ctx)).connector is True

    revoke_on_goto["enabled"] = True
    async def run_after_revocation():
        return await run_tool({"url": "https://allowed.example.test/dashboard"})

    events = _run(run_after_revocation())
    ending = [event for event in events if event.type == "tool.end"][-1]
    output = ending.payload["output"]
    assert output["success"] is False
    assert output["error_code"] == "preview_restricted"
    assert output.get("snapshot") is None
    assert ending.payload["observation"].get("snapshot") is None
    assert "Confidential dashboard" not in str(ending.payload)
    _run(manager.close_report(report["id"]))


async def _run_snapshot(tool, session_id, ctx):
    return [event async for event in tool.run_stream({"session_id": session_id}, ctx)]


@pytest.mark.e2e
def test_preview_reads_authenticated_api_without_a_backend_listener(
    monkeypatch, test_client, create_user, login_user, whoami, create_report,
    update_organization_settings,
):
    """Internal reads must work without a network server or backend URL setting."""
    import httpx
    from app.services.artifact_preview_service import ArtifactPreviewService

    report, token, org_id, user_id = _new_owner(create_user, login_user, whoami, create_report)
    artifact_id = _save_artifact(test_client, report["id"], token, org_id)
    _set_browser_settings(update_organization_settings, token, org_id, enabled=True)
    ctx = _run(_runtime_context(report["id"], user_id, org_id))
    monkeypatch.delenv("BOW_ARTIFACT_BACKEND_URL", raising=False)

    async def no_network(*args, **kwargs):
        raise httpx.ConnectError("Network connections are unavailable")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)

    async def read_preview():
        preview = ArtifactPreviewService(ctx)
        try:
            await preview.open(artifact_id)
            assert preview.artifact["id"] == artifact_id
            assert preview.artifact["report_id"] == report["id"]
            assert isinstance(preview.queries, list)
        finally:
            await preview.close()

    _run(read_preview())

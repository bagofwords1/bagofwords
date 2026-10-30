"""Automatic browser check after an interactive page create/edit.

The create/edit tools, the guarded browser tools, the browser policy, the
preview broker (real ASGI API), the process session manager and the DB all run
real. Only Chromium is replaced: a fake ``async_playwright`` beneath the real
``BrowserSessionManager`` (the seam used by ``test_artifact_browser_policy``),
plus the headless render validation of create/edit (as in
``test_create_artifact_replaces``).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from app.ai.tools import artifact_verification as av
from app.ai.tools.artifact_verification import ArtifactCheckRun
from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User

ROOT_WAIT_MESSAGE = (
    'Frame.wait_for_selector: Timeout 20000ms exceeded.\nCall log:\n'
    '  - waiting for locator("#root > *") to be visible\n'
)
REPLY_LINE = "- text: Preview answer. No agent was called."
PAGE_SNAPSHOT = '- heading "Sales explorer" [ref=e1]\n- button "Refresh" [ref=e2]'
CHAT_SNAPSHOT = '- heading "Team chat" [ref=e1]\n- textbox "Ask a question" [ref=e2]\n- button "Send" [ref=e3]'

INTERACTIVE_CODE = (
    '<script type="text/babel">function App() { const [n, setN] = React.useState(0); '
    'return <main><h1>Sales explorer</h1><button onClick={() => setN(n + 1)}>Refresh {n}</button></main> }</script>'
)
STATIC_CODE = '<script type="text/babel">function App() { return <main><h1>Sales</h1></main> }</script>'
CHAT_CODE = (
    '<script type="text/babel">function App() { const [q, setQ] = React.useState(""); '
    'const ask = async () => { const c = await bow.conversations.create({}); '
    'await c.send(q, { onEvent: (e) => setQ("") }); }; '
    'return <main><input value={q} onChange={(e) => setQ(e.target.value)} />'
    '<button onClick={ask}>Send</button></main> }</script>'
)
DESTRUCTURED_CHAT_CODE = (
    '<script type="text/babel">function App() { const [q, setQ] = React.useState(""); '
    'const ask = async () => { const { send } = await bow.conversations.create({}); '
    'await send(q); setQ(""); }; '
    'return <main><input value={q} onChange={(e) => setQ(e.target.value)} />'
    '<button onClick={ask}>Send</button></main> }</script>'
)
HOOK_CHAT_CODE = (
    '<script type="text/babel">function App() { const chat = useConversation(); '
    'const [q, setQ] = React.useState(""); '
    'return <main><input value={q} onChange={(e) => setQ(e.target.value)} />'
    '<button onClick={() => chat.ask(q)}>Send</button></main> }</script>'
)

pytestmark = pytest.mark.e2e


class PlaywrightError(Exception):
    pass


class FakeChromium:
    """Records what the real session manager and navigate/act code do to Chromium."""

    def __init__(self, behavior="healthy", snapshot=PAGE_SNAPSHOT):
        self.behavior = behavior
        self.snapshot = snapshot
        self.navigations = []
        self.acts = []
        self.contexts = []
        self.open_contexts = 0
        self.max_open_contexts = 0
        self.closed_while_busy = 0
        self.on_goto = None
        self.on_press = None
        self.on_context_close = None
        self.goto_console_error = None
        self.reply_text = None
        self.reply_with_answer = False
        self.real_async_playwright = None

    def factory(self):
        world = self

        class Factory:
            async def start(self):
                return FakePlaywright(world)

            async def __aenter__(self):
                self._real = world.real_async_playwright()
                return await self._real.__aenter__()

            async def __aexit__(self, *exc):
                return await self._real.__aexit__(*exc)

        return Factory()


class FakePlaywright:
    def __init__(self, world):
        self.chromium = FakeLauncher(world)

    async def stop(self):
        pass


class FakeLauncher:
    def __init__(self, world):
        self.world = world

    async def launch(self, **_kwargs):
        if self.world.behavior == "launch_fails":
            raise PlaywrightError("BrowserType.launch: Executable doesn't exist")
        return FakeBrowser(self.world)


class FakeBrowser:
    def __init__(self, world):
        self.world = world

    async def new_context(self, **_kwargs):
        context = FakeContext(self.world)
        self.world.contexts.append(context)
        self.world.open_contexts += 1
        self.world.max_open_contexts = max(self.world.max_open_contexts, self.world.open_contexts)
        return context

    async def close(self):
        pass


class FakeContext:
    def __init__(self, world):
        self.world = world
        self.closed = False
        self.busy = 0
        self.page = FakePage(world, self)

    async def route(self, *_args, **_kwargs):
        pass

    async def route_web_socket(self, *_args, **_kwargs):
        pass

    async def new_page(self):
        return self.page

    def on(self, *_args, **_kwargs):
        pass

    async def close(self):
        if self.closed:
            return
        if self.busy:
            self.world.closed_while_busy += 1
        if self.world.on_context_close:
            await self.world.on_context_close(self)
        self.closed = True
        self.world.open_contexts -= 1


class _Operation:
    """Playwright-like: operations on a closed context raise."""

    def __init__(self, context):
        self.context = context

    async def __aenter__(self):
        if self.context.closed:
            raise PlaywrightError("Target page, context or browser has been closed")
        self.context.busy += 1

    async def __aexit__(self, *exc):
        self.context.busy -= 1
        if exc[0] is None and self.context.closed:
            raise PlaywrightError("Target page, context or browser has been closed")


class FakePage:
    def __init__(self, world, context):
        self.world = world
        self.context = context
        self.url = "about:blank"
        self.main_frame = object()
        self.handlers = {}
        self.binding = None
        self.frame = FakeFrame(world, context, self)
        self.snapshot = world.snapshot

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def fire(self, event, value):
        for handler in self.handlers.get(event, []):
            handler(value)

    def console_error(self, text):
        self.fire("console", type("Console", (), {"type": "error", "text": text})())

    async def expose_binding(self, _name, callback):
        self.binding = callback

    async def add_init_script(self, *_args, **_kwargs):
        pass

    async def goto(self, url, **_kwargs):
        async with _Operation(self.context):
            self.url = url
            if "/artifact-preview/" in url:
                self.world.navigations.append(url)
            if self.world.on_goto:
                await self.world.on_goto(self)
            behavior = self.world.behavior
            if behavior == "hang":
                await asyncio.Event().wait()
            if behavior == "nav_refused":
                raise PlaywrightError(f"Page.goto: net::ERR_CONNECTION_REFUSED at {url}")
            if behavior == "crash_on_load":
                self.fire("pageerror", PlaywrightError("TypeError: Cannot read properties of undefined (reading 'map')"))
                return None
            if self.world.goto_console_error:
                self.console_error(self.world.goto_console_error)
            if behavior != "blank_root":
                artifact_id = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
                await self.binding({"frame": self.main_frame},
                                   {"artifact_id": artifact_id, "kind": "data_received", "data_revision": "r1"})
            return type("Response", (), {"status": 200})()

    async def wait_for_selector(self, selector, **_kwargs):
        async with _Operation(self.context):
            assert selector == "iframe[data-artifact-frame]"
            return FakeHandle(self.frame)

    async def title(self):
        return "Artifact preview"

    async def evaluate(self, *_args, **_kwargs):
        return []

    def locator(self, selector):
        return FakeLocator(self.frame, selector)


class FakeHandle:
    def __init__(self, frame):
        self.frame = frame

    async def content_frame(self):
        return self.frame


class FakeFrame:
    def __init__(self, world, context, page):
        self.world = world
        self.context = context
        self.page = page

    async def wait_for_selector(self, selector, **_kwargs):
        async with _Operation(self.context):
            assert selector == "#root > *"
            if self.world.behavior in {"crash_on_load", "blank_root"}:
                raise PlaywrightError(ROOT_WAIT_MESSAGE)

    async def evaluate(self, *_args, **_kwargs):
        async with _Operation(self.context):
            return []

    def locator(self, selector):
        return FakeLocator(self, selector)


class FakeLocator:
    def __init__(self, frame, selector):
        self.frame = frame
        self.selector = selector
        self.last = self

    async def count(self):
        return 0

    async def aria_snapshot(self, **_kwargs):
        async with _Operation(self.frame.context):
            if self.frame.world.behavior == "frozen_after_render":
                await asyncio.Event().wait()
            return self.frame.page.snapshot

    async def _act(self, action, value=None):
        page, world = self.frame.page, self.frame.world
        async with _Operation(self.frame.context):
            world.acts.append((action, self.selector, value))
            if action != "press":
                return
            if world.on_press:
                await world.on_press(page)
            behavior = world.behavior
            if behavior == "press_hangs":
                await asyncio.Event().wait()
            if behavior == "crash_on_reply":
                page.fire("pageerror", PlaywrightError("TypeError: Cannot read properties of undefined (reading 'text')"))
            elif behavior == "caught_reply_error":
                page.snapshot += "\n" + REPLY_LINE + "\n- alert: \"TypeError: Cannot read properties of undefined (reading 'text')\""
            elif behavior == "reply_ok":
                page.snapshot += "\n" + REPLY_LINE
            elif behavior == "reply_with_failed_text":
                page.snapshot += "\n" + REPLY_LINE + "\n- text: Sending failed, please retry"
            elif behavior == "reply_error_text":
                page.snapshot += ("\n" + REPLY_LINE if world.reply_with_answer else "") + f"\n- text: {world.reply_text}"

    async def fill(self, value, **_kwargs):
        await self._act("fill", value)

    async def press(self, key, **_kwargs):
        await self._act("press", key)

    async def click(self, **_kwargs):
        await self._act("click")


class CountingLock(asyncio.Lock):
    """Records lock acquisition attempts, and whether one met a held lock."""

    def __init__(self):
        super().__init__()
        self.contended = asyncio.Event()
        self.acquire_calls = 0

    async def acquire(self):
        self.acquire_calls += 1
        if self.locked():
            self.contended.set()
        return await super().acquire()


@pytest.fixture
def chromium(monkeypatch):
    import playwright.async_api

    world = FakeChromium()
    world.real_async_playwright = playwright.async_api.async_playwright
    monkeypatch.setattr(playwright.async_api, "async_playwright", world.factory)
    return world


@pytest.fixture
def stub_render(monkeypatch):
    """Headless render validation is a separate Chromium boundary; report clean renders."""
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool

    async def clean(self, code, artifact_data, mode, runtime_ctx, **kwargs):
        yield {"code": code, "clean": True, "screenshot": None, "errors": [], "repair_attempts": 0}

    async def no_screenshot(self, *_args, **_kwargs):
        return None, []

    monkeypatch.setattr(CreateArtifactTool, "_validate_and_repair_stream", clean)
    monkeypatch.setattr(CreateArtifactTool, "_take_preview_screenshot", no_screenshot)


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def owner(test_client, create_user, login_user, whoami, create_report, update_organization_settings):
    return _make_owner(test_client, create_user, login_user, whoami, create_report,
                       update_organization_settings, role="admin")


def _make_owner(test_client, create_user, login_user, whoami, create_report, update_organization_settings, *, role):
    admin = create_user(email=f"autocheck-{uuid.uuid4().hex[:10]}@example.com")
    admin_token = login_user(admin["email"], admin["password"])
    identity = whoami(admin_token)
    org_id = identity["organizations"][0]["id"]
    token, user_id = admin_token, identity["id"]
    if role == "member":
        email = f"autocheck-member-{uuid.uuid4().hex[:10]}@example.com"
        invite = test_client.post(f"/api/organizations/{org_id}/members",
                                  json={"organization_id": org_id, "email": email, "role": "member"},
                                  headers=_headers(admin_token, org_id))
        assert invite.status_code == 200, invite.json()
        member = create_user(email=email, password="test123")
        token = login_user(member["email"], member["password"])
        user_id = whoami(token)["id"]
    report = create_report(title="Automatic check", user_token=token, org_id=org_id, data_sources=[])

    def settings(**config):
        update_organization_settings(config, user_token=admin_token, org_id=org_id)

    settings(enable_artifact_verification=True, allow_llm_see_data=True)
    return {"report_id": report["id"], "org_id": org_id, "user_id": user_id, "settings": settings}


async def _context(db, owner):
    report = await db.get(Report, owner["report_id"])
    user = await db.get(User, owner["user_id"])
    organization = await db.get(Organization, owner["org_id"])
    return {"db": db, "report": report, "user": user, "organization": organization}


async def _drive(tool, tool_input, ctx):
    events = [event async for event in tool.run_stream(tool_input, ctx)]
    ends = [event for event in events if event.type == "tool.end"]
    assert len(ends) == 1, [event.type for event in events]
    return events, ends[0].payload


def _create_input(code, title="Sales explorer"):
    return {"prompt": "build", "mode": "page", "code": code, "title": title}


async def _create(owner, code=INTERACTIVE_CODE, *, run=True, execution_id=None, smoke_allowed=True,
                  prepare=None, title="Sales explorer"):
    """One create_artifact call. ``run`` False omits the run state (MCP-style caller)."""
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool

    async with async_session_maker() as db:
        ctx = await _context(db, owner)
        ctx["agent_execution_id"] = execution_id or str(uuid.uuid4())
        if run is True:
            run = ArtifactCheckRun(execution_id=ctx["agent_execution_id"], smoke_allowed=smoke_allowed)
        if run is not None and run is not False:
            ctx["artifact_check_run"] = run
        if prepare:
            prepare(ctx)
        events, payload = await _drive(CreateArtifactTool(), _create_input(code, title), ctx)
        return events, payload, ctx


def _check(payload):
    output_hint = payload["output"]["verification_hint"]
    observation_hint = payload["observation"]["verification_hint"]
    assert observation_hint.get("automatic_check") == output_hint.get("automatic_check")
    return output_hint.get("automatic_check")


def _auto_events(events):
    return [event for event in events if (event.payload or {}).get("check") == "automatic_browser"]


def _version_exists(version_id):
    async def read():
        from app.services.artifact_service import ArtifactService
        async with async_session_maker() as db:
            return await ArtifactService().get(db, version_id) is not None
    return asyncio.run(read())


def _no_open_preview(execution_id):
    from app.ai.tools.implementations._browser_common import session_manager
    return not [s for s in session_manager._sessions.values()
                if s.preview and s.scope and s.scope[-1] == str(execution_id)]


@pytest.mark.parametrize("role", ["admin", "member"])
def test_interactive_page_runs_exactly_one_check_and_reports_pass(
    role, chromium, stub_render, test_client, create_user, login_user, whoami, create_report,
    update_organization_settings,
):
    owner = _make_owner(test_client, create_user, login_user, whoami, create_report,
                        update_organization_settings, role=role)
    events, payload, ctx = asyncio.run(_create(owner))

    check = _check(payload)
    assert check is not None, "create_artifact returned no automatic_check"
    assert check["status"] == "passed"
    assert check["checked"] == ["load"]
    assert check["artifact_id"] == payload["output"]["artifact_id"]
    assert len(chromium.navigations) == 1
    assert chromium.contexts and all(context.closed for context in chromium.contexts)
    assert _no_open_preview(ctx["agent_execution_id"])
    assert payload["output"].get("success") is not False
    assert _version_exists(payload["output"]["artifact_id"])
    # D2: the only added events are the two progress events; inner browser
    # events and evidence never reach the runner.
    added = _auto_events(events)
    assert [event.type for event in added] == ["tool.progress", "tool.progress"]
    assert added[1].payload.get("timing") is False
    assert [event.type for event in events].count("tool.start") == 1
    assert not any(event.payload.get("title") == "Checking the saved app" for event in events)
    for event in events:
        for part in (event.payload, event.payload.get("output") or {}, event.payload.get("observation") or {}):
            assert not {"snapshot", "evidence", "session_id"} & set(part)


def test_load_crash_is_reported_to_the_agent(chromium, stub_render, owner):
    chromium.behavior = "crash_on_load"
    _events, payload, _ctx = asyncio.run(_create(owner))

    check = _check(payload)
    assert check["status"] == "problems_found"
    assert check["errors"][0]["source"] == "browser"
    assert payload["observation"]["verification_hint"]["automatic_check"] is check
    assert "edit_artifact" in payload["observation"]["summary"]
    assert payload["output"].get("success") is not False


def test_static_dashboard_is_not_checked(chromium, stub_render, owner):
    events, payload, _ctx = asyncio.run(_create(owner, STATIC_CODE))

    assert payload["output"]["verification_hint"]["recommended"] is False
    assert "automatic_check" not in payload["output"]["verification_hint"]
    assert chromium.navigations == []
    assert _auto_events(events) == []


@pytest.mark.parametrize("config", [
    {"enable_artifact_verification": False}, {"allow_llm_see_data": False},
], ids=["verification_off", "privacy_off"])
def test_setting_off_or_privacy_off_means_no_check(config, chromium, stub_render, owner):
    owner["settings"](**config)
    events, payload, _ctx = asyncio.run(_create(owner))

    hint = payload["output"]["verification_hint"]
    assert hint["availability"] == "unavailable"
    assert "automatic_check" not in hint
    assert chromium.navigations == []
    assert _auto_events(events) == []


@pytest.mark.parametrize("behavior", ["launch_fails", "nav_refused"])
def test_browser_unavailable_degrades_to_advisory(behavior, chromium, stub_render, owner):
    chromium.behavior = behavior

    async def scenario():
        run = ArtifactCheckRun(execution_id=str(uuid.uuid4()), smoke_allowed=True)
        _events, payload, _ctx = await _create(owner, run=run, execution_id=run.execution_id)
        return payload, run

    payload, run = asyncio.run(scenario())
    check = _check(payload)
    assert check["status"] == "unavailable"
    assert check["reason"] in {"preview_unavailable", "preview_not_ready"}
    assert payload["output"]["verification_hint"]["recommended"] is True
    assert payload["output"].get("success") is not False
    assert run.unavailable_reason is not None


def test_blank_render_is_an_app_problem_and_does_not_trip_the_breaker(chromium, stub_render, owner):
    async def scenario():
        run = ArtifactCheckRun(execution_id=str(uuid.uuid4()), smoke_allowed=True)
        chromium.behavior = "blank_root"
        _e, first, _c = await _create(owner, run=run, execution_id=run.execution_id, title="First")
        breaker_after_first = run.unavailable_reason
        chromium.behavior = "healthy"
        _e, second, _c = await _create(owner, run=run, execution_id=run.execution_id, title="Second")
        return first, second, breaker_after_first

    first, second, breaker_after_first = asyncio.run(scenario())
    first_check = _check(first)
    assert first_check["status"] == "problems_found"
    assert first_check["errors"][-1]["source"] == "render"
    assert breaker_after_first is None
    assert "edit_artifact" in first["observation"]["summary"]
    assert _check(second)["status"] == "passed"
    assert len(chromium.navigations) == 2


def test_chat_app_stream_crash_is_caught_by_fixture_smoke(chromium, stub_render, owner):
    chromium.behavior, chromium.snapshot = "crash_on_reply", CHAT_SNAPSHOT
    _events, payload, ctx = asyncio.run(_create(owner, CHAT_CODE))

    check = _check(payload)
    assert check["status"] == "problems_found"
    assert check["conversation_smoke"]["status"] == "problems_found"
    assert any(error["source"] == "browser" and "TypeError" in error["message"] for error in check["errors"])
    assert all(context.closed for context in chromium.contexts)
    assert _no_open_preview(ctx["agent_execution_id"])


def test_caught_and_rendered_reply_error_is_not_a_pass(chromium, stub_render, owner):
    chromium.behavior, chromium.snapshot = "caught_reply_error", CHAT_SNAPSHOT
    _events, payload, _ctx = asyncio.run(_create(owner, CHAT_CODE))

    check = _check(payload)
    assert check["status"] == "problems_found"
    assert check["conversation_smoke"]["status"] == "problems_found"
    assert any(error["source"] == "ui" and "TypeError" in error["message"] for error in check["errors"])
    assert "conversation_send" in check["checked"]


@pytest.mark.parametrize(("behavior", "snapshot", "smoke_allowed", "smoke", "checked"), [
    ("reply_ok", CHAT_SNAPSHOT, True, ("passed", None), ["load", "conversation_send"]),
    ("reply_ok", '- heading "Team chat" [ref=e1]\n- button "Send" [ref=e3]', True,
     ("not_run", "no_composer"), ["load"]),
    ("reply_ok", CHAT_SNAPSHOT, False, ("not_run", "act_not_allowed"), ["load"]),
], ids=["round_trip", "no_composer", "act_not_allowed"])
def test_chat_app_round_trip_passes_and_unclear_cases_do_not(
    behavior, snapshot, smoke_allowed, smoke, checked, chromium, stub_render, owner,
):
    chromium.behavior, chromium.snapshot = behavior, snapshot
    _events, payload, _ctx = asyncio.run(_create(owner, CHAT_CODE, smoke_allowed=smoke_allowed))

    check = _check(payload)
    assert check["status"] == "passed"
    assert (check["conversation_smoke"]["status"], check["conversation_smoke"]["reason"]) == smoke
    assert check["checked"] == checked
    if not smoke_allowed:
        assert chromium.acts == []


def _assert_withheld(check, *markers, payload):
    assert (check["status"], check["reason"]) == ("unavailable", "restricted")
    assert check["errors"] == [] and check["failed_queries"] == [] and check["checked"] == []
    assert check["readiness"] is None and check["conversation_smoke"] is None
    for marker in markers:
        assert marker not in str(payload)


def test_revoked_access_during_load_withholds_evidence(chromium, stub_render, owner):
    marker = f"secret-{uuid.uuid4().hex}"

    async def revoke(page):
        owner["settings"](allow_llm_see_data=False)
        page.fire("pageerror", PlaywrightError(f"TypeError: {marker}"))

    chromium.on_goto = revoke
    _events, payload, _ctx = asyncio.run(_create(owner))
    _assert_withheld(_check(payload), marker, payload=payload)


@pytest.mark.parametrize("role", ["admin", "member"])
def test_revoked_access_during_smoke_act_withholds_everything(
    role, chromium, stub_render, test_client, create_user, login_user, whoami, create_report,
    update_organization_settings,
):
    owner = _make_owner(test_client, create_user, login_user, whoami, create_report,
                        update_organization_settings, role=role)
    marker = f"secret-{uuid.uuid4().hex}"
    chromium.behavior, chromium.snapshot = "reply_ok", CHAT_SNAPSHOT

    async def revoke(page):
        owner["settings"](allow_llm_see_data=False)
        page.fire("pageerror", PlaywrightError(f"TypeError: {marker}"))

    chromium.on_press = revoke
    _events, payload, _ctx = asyncio.run(_create(owner, CHAT_CODE))
    assert any(action == "press" for action, _selector, _value in chromium.acts)
    _assert_withheld(_check(payload), marker, "No agent was called", payload=payload)


def test_revocation_before_attach_is_caught_by_final_check(chromium, stub_render, owner):
    async def revoke(_context):
        owner["settings"](allow_llm_see_data=False)

    chromium.on_context_close = revoke
    _events, payload, _ctx = asyncio.run(_create(owner))
    _assert_withheld(_check(payload), payload=payload)


def test_hung_cleanup_or_final_access_check_is_bounded_and_withholds_evidence(
    monkeypatch, chromium, stub_render, owner,
):
    from app.ai.tools.implementations._browser_common import session_manager

    marker = f"secret-{uuid.uuid4().hex}"
    chromium.goto_console_error = f"Uncaught Error: {marker}"
    monkeypatch.setattr(av, "AUTOMATIC_CHECK_CLEANUP_TIMEOUT_S", 0.5)

    async def scenario():
        never = asyncio.Event()

        async def hang(_context):
            await never.wait()

        chromium.on_context_close = hang
        try:
            return await _create(owner)
        finally:
            chromium.on_context_close = None
            never.set()

    _events, payload, ctx = asyncio.run(scenario())
    try:
        check = _check(payload)
        assert (check["status"], check["reason"]) == ("unavailable", "check_failed")
        assert check["errors"] == [] and check["failed_queries"] == [] and check["checked"] == []
        assert check["readiness"] is None and check["conversation_smoke"] is None
        assert marker not in str(payload)
        assert payload["output"].get("success") is not False
    finally:
        asyncio.run(session_manager.close_execution(ctx["agent_execution_id"]))


def test_load_timeout_is_unavailable_and_trips_the_breaker(monkeypatch, chromium, stub_render, owner):
    chromium.behavior = "hang"
    monkeypatch.setattr(av, "AUTOMATIC_CHECK_TIMEOUT_S", 0.5)

    async def scenario():
        run = ArtifactCheckRun(execution_id=str(uuid.uuid4()), smoke_allowed=True)
        _events, payload, _ctx = await _create(owner, run=run, execution_id=run.execution_id)
        return payload, run

    payload, run = asyncio.run(scenario())
    check = _check(payload)
    assert (check["status"], check["reason"]) == ("unavailable", "timeout")
    assert chromium.contexts and all(context.closed for context in chromium.contexts)
    assert payload["output"].get("success") is not False
    assert run.unavailable_reason == "timeout"


def test_no_check_starts_after_the_latest_start_time(monkeypatch, chromium, stub_render, owner):
    monkeypatch.setattr(av, "AUTOMATIC_CHECK_LATEST_START_S", 0)
    _events, payload, _ctx = asyncio.run(_create(owner))

    assert (_check(payload)["status"], _check(payload)["reason"]) == ("unavailable", "time_budget")
    assert chromium.navigations == []


def test_smoke_timeout_keeps_the_load_result(monkeypatch, chromium, stub_render, owner):
    chromium.behavior, chromium.snapshot = "press_hangs", CHAT_SNAPSHOT
    monkeypatch.setattr(av, "AUTOMATIC_CHECK_SMOKE_TIMEOUT_S", 0.5)
    _events, payload, _ctx = asyncio.run(_create(owner, CHAT_CODE))

    check = _check(payload)
    assert check["status"] == "passed"
    assert (check["conversation_smoke"]["status"], check["conversation_smoke"]["reason"]) == ("inconclusive", "timeout")
    assert check["checked"] == ["load"]
    assert chromium.contexts and all(context.closed for context in chromium.contexts)


def test_lock_wait_expiry_is_a_time_budget_skip(monkeypatch, chromium, stub_render, owner):
    monkeypatch.setattr(av, "AUTOMATIC_CHECK_LOCK_WAIT_S", 0.05)

    async def scenario():
        run = ArtifactCheckRun(execution_id=str(uuid.uuid4()), smoke_allowed=True, lock=CountingLock())
        await run.lock.acquire()
        try:
            _events, payload, _ctx = await _create(owner, run=run, execution_id=run.execution_id)
            still_held = run.lock.locked()
        finally:
            run.lock.release()
        return payload, still_held, run.lock.acquire_calls

    payload, still_held, acquire_calls = asyncio.run(scenario())
    assert (_check(payload)["status"], _check(payload)["reason"]) == ("unavailable", "time_budget")
    assert chromium.navigations == []
    assert still_held is True
    assert acquire_calls == 2  # the test's own acquire, then the check's bounded wait


def test_lock_wait_is_bounded_by_the_remaining_start_budget(chromium, stub_render, owner):
    async def scenario():
        run = ArtifactCheckRun(execution_id=str(uuid.uuid4()), smoke_allowed=True, lock=CountingLock())
        await run.lock.acquire()
        run.lock.contended.clear()

        def seed(ctx):
            ctx["_artifact_check_t0"] = time.monotonic() - (av.AUTOMATIC_CHECK_LATEST_START_S - 2.0)

        try:
            async with asyncio.timeout(10):
                _events, payload, _ctx = await _create(owner, run=run, execution_id=run.execution_id, prepare=seed)
        finally:
            run.lock.release()
        return payload, run.lock.contended.is_set()

    payload, contended = asyncio.run(scenario())
    assert (_check(payload)["status"], _check(payload)["reason"]) == ("unavailable", "time_budget")
    assert chromium.navigations == []
    assert contended, "create path exceeded the 2 s window"


def test_retry_clock_is_not_reset_and_skips_without_waiting(chromium, stub_render, owner):
    seeded = time.monotonic() - 1000

    async def scenario():
        run = ArtifactCheckRun(execution_id=str(uuid.uuid4()), smoke_allowed=True, lock=CountingLock())

        def seed(ctx):
            ctx["_artifact_check_t0"] = seeded

        async with asyncio.timeout(10):
            _events, payload, ctx = await _create(owner, run=run, execution_id=run.execution_id, prepare=seed)
        return payload, ctx, run.lock.acquire_calls

    payload, ctx, acquire_calls = asyncio.run(scenario())
    assert (_check(payload)["status"], _check(payload)["reason"]) == ("unavailable", "time_budget")
    assert chromium.navigations == []
    assert ctx["_artifact_check_t0"] == seeded
    assert acquire_calls == 0


def test_functional_edit_is_checked_cosmetic_edit_is_not(chromium, stub_render, owner):
    from app.ai.tools.implementations.edit_artifact import EditArtifactTool

    async def scenario():
        _events, created, _ctx = await _create(owner, run=None)
        artifact_id = created["output"]["artifact_id"]
        navigations_after_create = len(chromium.navigations)
        async with async_session_maker() as db:
            ctx = await _context(db, owner)
            ctx["agent_execution_id"] = str(uuid.uuid4())
            ctx["artifact_check_run"] = ArtifactCheckRun(execution_id=ctx["agent_execution_id"], smoke_allowed=True)
            _e, functional = await _drive(EditArtifactTool(), {"artifact_id": artifact_id, "edits": [
                {"find": "setN(n + 1)", "replace": "setN(n + 2)"}]}, ctx)
            navigations_after_functional = len(chromium.navigations)
            _e, cosmetic = await _drive(EditArtifactTool(), {"artifact_id": functional["output"]["artifact_id"], "edits": [
                {"find": "<h1>Sales explorer</h1>", "replace": "<h1>Revenue explorer</h1>"}]}, ctx)
        return navigations_after_create, functional, navigations_after_functional, cosmetic

    after_create, functional, after_functional, cosmetic = asyncio.run(scenario())
    assert after_create == 0
    assert functional["output"].get("success") is True, functional
    assert _check(functional)["status"] == "passed"
    assert after_functional == 1
    assert cosmetic["output"].get("success") is True, cosmetic
    assert "automatic_check" not in cosmetic["output"]["verification_hint"]
    assert len(chromium.navigations) == 1


@pytest.mark.parametrize("run_state", ["absent", "plain_lock"])
def test_callers_without_run_state_never_check(run_state, chromium, stub_render, owner):
    async def scenario():
        run = None if run_state == "absent" else asyncio.Lock()
        return await _create(owner, run=run)

    events, payload, _ctx = asyncio.run(scenario())
    assert payload["output"]["verification_hint"]["recommended"] is True
    assert "automatic_check" not in payload["output"]["verification_hint"]
    assert chromium.navigations == []
    assert _auto_events(events) == []


def test_concurrent_checks_in_one_run_are_serialized(chromium, stub_render, owner):
    async def scenario():
        run = ArtifactCheckRun(execution_id=str(uuid.uuid4()), smoke_allowed=True, lock=CountingLock())
        second_goto = asyncio.Event()
        gotos = {"n": 0}

        async def barrier(_page):
            gotos["n"] += 1
            if gotos["n"] > 1:
                second_goto.set()
                return
            waiters = [asyncio.ensure_future(run.lock.contended.wait()), asyncio.ensure_future(second_goto.wait())]
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            for waiter in waiters:
                waiter.cancel()

        chromium.on_goto = barrier
        results = await asyncio.gather(
            _create(owner, run=run, execution_id=run.execution_id, title="First"),
            _create(owner, run=run, execution_id=run.execution_id, title="Second"),
        )
        return [payload for _events, payload, _ctx in results]

    payloads = asyncio.run(scenario())
    assert [_check(payload)["status"] for payload in payloads] == ["passed", "passed"]
    assert chromium.closed_while_busy == 0
    assert len(chromium.navigations) == 2
    assert chromium.max_open_contexts == 1


def test_infra_failure_trips_the_run_breaker(chromium, stub_render, owner):
    chromium.behavior = "nav_refused"

    async def scenario():
        run = ArtifactCheckRun(execution_id=str(uuid.uuid4()), smoke_allowed=True)
        _e, first, _c = await _create(owner, run=run, execution_id=run.execution_id, title="First")
        _e, second, _c = await _create(owner, run=run, execution_id=run.execution_id, title="Second")
        return first, second

    first, second = asyncio.run(scenario())
    assert (_check(first)["status"], _check(first)["reason"]) == ("unavailable", "preview_not_ready")
    assert (_check(second)["status"], _check(second)["reason"]) == ("unavailable", "skipped_after_unavailable")
    assert len(chromium.navigations) == 1


def test_shared_db_session_is_released_before_the_browser_work(chromium, stub_render, owner):
    observed = []

    async def scenario():
        holder = {}

        def add_lock(ctx):
            ctx["tool_db_lock"] = asyncio.Lock()
            holder["ctx"] = ctx

        async def record(_page):
            observed.append(holder["ctx"]["db"].in_transaction())

        chromium.on_goto = record
        _events, payload, ctx = await _create(owner, prepare=add_lock)
        return payload, ctx["tool_db_lock"].locked()

    payload, lock_held = asyncio.run(scenario())
    assert observed == [False]
    assert _check(payload)["status"] == "passed"
    assert lock_held is False


def test_busy_db_lock_does_not_block_the_check(monkeypatch, chromium, stub_render, owner):
    monkeypatch.setattr(av, "AUTOMATIC_CHECK_DB_RELEASE_WAIT_S", 0.5)

    async def scenario():
        lock = asyncio.Lock()
        await lock.acquire()

        def add_lock(ctx):
            ctx["tool_db_lock"] = lock

        try:
            async with asyncio.timeout(10):
                _events, payload, _ctx = await _create(owner, prepare=add_lock)
            still_held = lock.locked()
        finally:
            lock.release()
        return payload, still_held

    payload, still_held = asyncio.run(scenario())
    assert _check(payload)["status"] == "passed"
    assert len(chromium.navigations) == 1
    assert still_held is True


@pytest.mark.parametrize(("behavior", "text"), [
    ("reply_error_text", "Failed to send message. Please try again."),
    ("reply_error_text", "Sorry, something broke."),
    ("reply_error_text", "Oops! Couldn't get a reply."),
    ("reply_with_failed_text", "Sending failed, please retry"),
], ids=["failed_to_send", "something_broke", "couldnt_get_reply", "reply_then_failed_text"])
def test_error_text_after_the_test_send_is_a_problem(behavior, text, chromium, stub_render, owner):
    # The fixture SDK always replies at once, so error text after the send is
    # the app's own send path failing: never a pass.
    chromium.behavior, chromium.snapshot, chromium.reply_text = behavior, CHAT_SNAPSHOT, text
    _events, payload, _ctx = asyncio.run(_create(owner, CHAT_CODE))

    check = _check(payload)
    assert check["status"] == "problems_found"
    assert check["conversation_smoke"]["status"] == "problems_found"
    assert any(error["source"] == "ui" and text in error["message"] for error in check["errors"])
    assert check["checked"] == ["load", "conversation_send"]
    assert "edit_artifact" in payload["observation"]["summary"]


def test_no_reply_after_the_test_send_is_not_a_pass(chromium, stub_render, owner):
    chromium.behavior, chromium.snapshot = "healthy", CHAT_SNAPSHOT
    _events, payload, _ctx = asyncio.run(_create(owner, CHAT_CODE))

    check = _check(payload)
    assert (check["status"], check["reason"]) == ("not_verified", "no_reply")
    assert (check["conversation_smoke"]["status"], check["conversation_smoke"]["reason"]) == ("inconclusive", "no_reply")
    assert check["checked"] == ["load"]
    assert [action for action, _selector, _value in chromium.acts] == ["fill", "press", "click"]
    summary = payload["observation"]["summary"]
    assert "no reply" in summary
    assert "no runtime errors were observed" not in summary


@pytest.mark.parametrize("code", [DESTRUCTURED_CHAT_CODE, HOOK_CHAT_CODE], ids=["destructured_send", "use_conversation"])
def test_component_and_destructured_chat_apps_get_the_smoke(code, chromium, stub_render, owner):
    chromium.behavior, chromium.snapshot = "reply_ok", CHAT_SNAPSHOT
    _events, payload, _ctx = asyncio.run(_create(owner, code))

    check = _check(payload)
    assert check["status"] == "passed"
    assert (check["conversation_smoke"]["status"], check["checked"]) == ("passed", ["load", "conversation_send"])


def test_a_non_chat_app_reports_the_smoke_as_not_detected(chromium, stub_render, owner):
    _events, payload, _ctx = asyncio.run(_create(owner))

    check = _check(payload)
    assert check["status"] == "passed"
    assert check["conversation_smoke"] == {"status": "not_run", "reason": "not_detected"}
    assert "test message" not in payload["observation"]["summary"]
    assert chromium.acts == []


def test_an_error_rendered_on_load_is_a_ui_problem(chromium, stub_render, owner):
    # An error boundary renders the crash inside an alert; the unnamed wrapper
    # div is dropped by the snapshot filter, and its text stays in the alert.
    chromium.snapshot = (PAGE_SNAPSHOT + '\n- alert [ref=e3]:\n  - generic [ref=e4]:\n'
                         '    - generic [ref=e5]: "TypeError: rows.map is not a function"')
    _events, payload, _ctx = asyncio.run(_create(owner))

    check = _check(payload)
    assert check["status"] == "problems_found"
    assert [error["source"] for error in check["errors"]] == ["ui"]
    assert "rows.map is not a function" in check["errors"][0]["message"]
    assert check["checked"] == ["load"]


def test_error_words_in_a_log_list_beside_an_alert_pass_the_load(chromium, stub_render, owner):
    chromium.snapshot = (PAGE_SNAPSHOT + '\n- alert [ref=e3]: 12 failed logins\n- list [ref=e4]:\n'
                         '  - listitem [ref=e5]: "TypeError: rows.map is not a function"')
    _events, payload, _ctx = asyncio.run(_create(owner))

    check = _check(payload)
    assert (check["status"], check["errors"]) == ("passed", [])


def test_a_page_that_freezes_after_rendering_is_an_app_problem_not_an_outage(
    monkeypatch, chromium, stub_render, owner,
):
    chromium.behavior = "frozen_after_render"
    chromium.goto_console_error = "Uncaught Error: render loop"
    monkeypatch.setattr(av, "AUTOMATIC_CHECK_TIMEOUT_S", 0.5)

    async def scenario():
        run = ArtifactCheckRun(execution_id=str(uuid.uuid4()), smoke_allowed=True)
        _events, payload, ctx = await _create(owner, run=run, execution_id=run.execution_id)
        return payload, run, ctx

    payload, run, ctx = asyncio.run(scenario())
    check = _check(payload)
    assert check["status"] == "problems_found"
    assert check["errors"][0] == {"source": "console", "message": "Uncaught Error: render loop"}
    assert check["errors"][-1]["source"] == "render"
    assert "stopped responding" in check["errors"][-1]["message"]
    assert check["checked"] == ["load"]
    assert run.unavailable_reason is None
    assert chromium.contexts and all(context.closed for context in chromium.contexts)
    assert _no_open_preview(ctx["agent_execution_id"])


@pytest.fixture
def check_logs(monkeypatch, caplog):
    # The per-test migration run (alembic's fileConfig) disables loggers that
    # already exist; the app never runs it in-process.
    monkeypatch.setattr(av.logger, "disabled", False)
    return caplog


def test_check_logs_carry_ids_and_withheld_results_log_only_their_status(check_logs, chromium, stub_render, owner):
    caplog = check_logs
    marker = f"secret-{uuid.uuid4().hex}"
    chromium.goto_console_error = f"Uncaught Error: {marker}"  # a real problems_found load

    async def revoke(_context):  # caught by the final access check
        owner["settings"](allow_llm_see_data=False)

    def ids(ctx):
        ctx["tool_call_id"] = "tool-call-1"

    chromium.on_context_close = revoke
    with caplog.at_level("INFO", logger=av.logger.name):
        _events, payload, ctx = asyncio.run(_create(owner, prepare=ids))
    messages = [record.getMessage() for record in caplog.records if record.name == av.logger.name]
    assert marker not in " ".join(messages)
    withheld = [m for m in messages if "withheld" in m]
    assert withheld and "status=problems_found" in withheld[0]
    final = [m for m in messages if m.startswith("automatic_artifact_check ")]
    assert final, messages
    assert f"execution_id={ctx['agent_execution_id']}" in final[-1]
    assert f"version={payload['output']['verification_hint']['version']}" in final[-1]
    assert "tool_call_id=tool-call-1" in final[-1]
    _assert_withheld(_check(payload), marker, payload=payload)


def test_gate_skips_are_logged_with_their_reason(check_logs, chromium, stub_render, owner):
    caplog = check_logs
    with caplog.at_level("DEBUG", logger=av.logger.name):
        asyncio.run(_create(owner, STATIC_CODE))
    messages = [record.getMessage() for record in caplog.records if record.name == av.logger.name]
    assert any("automatic_artifact_check skipped" in m and "reason=not_recommended" in m for m in messages), messages


def test_the_check_s_preview_activity_is_not_reported_again_next_turn(chromium, stub_render, owner):
    """The check's own preview events (logged as source=preview) are already in
    this completion's tool result: they are stamped as shown in it, so the
    next turn's <app_activity> does not repeat them."""
    from sqlalchemy import text

    from app.services import artifact_activity

    chromium.behavior, chromium.snapshot = "reply_ok", CHAT_SNAPSHOT
    completion_id = str(uuid.uuid4())

    async def refused_create(page):
        ref = urlparse(page.url).path.rstrip("/").rsplit("/", 1)[-1]
        await page.binding({"frame": page.main_frame}, {
            "artifact_id": ref, "kind": "sdk_call", "event_kind": "request", "op": "conversations.create",
            "status": "error", "error_code": "conversation.agent_forbidden",
            "detail": {"message": "You can't use that agent here"}})

    chromium.on_goto = refused_create
    _events, payload, ctx = asyncio.run(_create(
        owner, CHAT_CODE, prepare=lambda c: c.update(system_completion=SimpleNamespace(id=completion_id))))

    check = _check(payload)
    assert check["status"] == "problems_found"
    assert any(e["source"] == "sdk" and "conversation.agent_forbidden" in e["message"] for e in check["errors"])

    async def logged():
        async with async_session_maker() as db:
            rows = (await db.execute(text(
                "select source, error_code, shown_in_completion_id from artifact_activity_events "
                "where report_id = :r"), {"r": owner["report_id"]})).all()
            section = await artifact_activity.context_section(db, report_id=owner["report_id"],
                                                              completion_id=str(uuid.uuid4()))
            return [tuple(r) for r in rows], section

    rows, section = asyncio.run(logged())
    assert ("preview", "conversation.agent_forbidden", completion_id) in rows
    assert all(shown == completion_id for _source, _code, shown in rows)
    assert section is None, section

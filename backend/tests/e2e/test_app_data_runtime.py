"""The artifact app-data runtime (`useCollection`) against a real browser.

Pins the runtime half of the app persistence contract (frontend/public/libs/
artifact-globals.js) and the hosts that never talk to a server:

  * transport selection per request: `window.__bowAppDataHost` when defined,
    the built-in in-memory host on a top-level page (headless validation,
    thumbnails, PDF), otherwise `APP_DATA_REQUEST` posted to the parent;
  * the iframe bridge accepts an `APP_DATA_RESULT` only from `window.parent`
    and only for a pending `rid`;
  * a failed write rejects with a tagged `AppDataError` that never reaches the
    host's error boundary as `ARTIFACT_ERROR` and never surfaces as a page
    error, while `error.code` renders;
  * `conflict` refreshes the collection; no answer ends in `timeout`;
  * the HTML export and the MCP app answer `unavailable` instead of hanging;
  * an artifact that never calls `useCollection` sends no request at all.

Needs Playwright plus the vendored JS libs and skips when either is absent
(same probe as test_html_export_offline.py).

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/test_app_data_runtime.py -q
"""
import json
import re
from pathlib import Path

import pytest

from tests.e2e.test_html_export_offline import BROWSER_AVAILABLE

pytestmark = pytest.mark.e2e

requires_browser = pytest.mark.skipif(
    not BROWSER_AVAILABLE,
    reason="needs Playwright + a downloaded Chromium + the vendored JS libs "
           "(scripts/download-vendor-libs.sh)",
)

ROOT = Path(__file__).resolve().parents[3]
IFRAME_TS = ROOT / "frontend" / "utils" / "artifactIframe.ts"
MCP_HTML = ROOT / "frontend" / "public" / "mcp-artifact-app.html"

VIEWER = {"id": "user-ada", "name": "Ada Viewer"}

# No .catch on any write on purpose: an uncaught app-data rejection must not
# blank the dashboard (PP14). Two components share one collection to prove the
# store issues a single list for both.
NOTES_APP = """<script type="text/babel">
function Notes() {
  const notes = useCollection('notes');
  window.__notes = notes;  // lets a test drive the API directly
  const first = notes.items[0];
  return (
    <div>
      <div id="loading">{notes.loading ? 'yes' : 'no'}</div>
      <div id="error">{notes.error ? notes.error.code : 'none'}</div>
      <div id="count">{notes.items.length}</div>
      <ul>
        {notes.items.map(n => (
          <li key={n.id} className="note" data-version={n.version} data-mine={String(n.mine)}
              data-user={n.user ? n.user.name : ''}>{n.data.text}</li>
        ))}
      </ul>
      <button id="add" onClick={() => { notes.add({ text: 'new note' }); }}>add</button>
      <button id="update" onClick={() => { if (first) notes.update(first.id, { text: 'edited' }); }}>update</button>
      <button id="remove" onClick={() => { if (first) notes.remove(first.id); }}>remove</button>
    </div>
  );
}
function Counter() {
  const notes = useCollection('notes');
  return <div id="count2">{notes.items.length}</div>;
}
function App() { return <main><Notes /><Counter /></main>; }
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>"""

PLAIN_APP = """<script type="text/babel">
function App() {
  const { values } = useParams();
  return <main id="plain">Plain dashboard {Object.keys(values).length}</main>;
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>"""

# The export has no Babel: the artifact arrives pre-transpiled.
EXPORT_APP_JS = """<script>
(function () {
  var h = React.createElement;
  function App() {
    var notes = useCollection('notes');
    return h('div', null,
      h('div', { id: 'loading' }, notes.loading ? 'yes' : 'no'),
      h('div', { id: 'error' }, notes.error ? notes.error.code : 'none'),
      h('div', { id: 'count' }, String(notes.items.length)));
  }
  ReactDOM.createRoot(document.getElementById('root')).render(h(App));
})();
</script>"""


def _error_boundary_script() -> str:
    """The host's real error boundary, injected after the runtime in every
    in-app iframe (utils/artifactIframe.ts)."""
    src = IFRAME_TS.read_text(encoding="utf-8")
    m = re.search(r"function errorBoundaryScript\(\)[^{]*\{\s*return `(.*?)`;\s*\}", src, re.S)
    assert m, "errorBoundaryScript not found in artifactIframe.ts"
    return m.group(1)


def _json_for_script(value) -> str:
    return json.dumps(value).replace("</", "<\\/")


def _artifact_page(code: str, *, before_runtime: str = "", boundary: bool = False) -> str:
    """A page shaped like the headless/in-app render: data, runtime, code."""
    from app.services.artifact_libs import get_inline_scripts

    data = {"visualizations": [], "current_user": VIEWER, "runtime": {"version": 11}}
    boundary_tag = f"<script>{_error_boundary_script()}</script>" if boundary else ""
    return (
        "<!DOCTYPE html><html><head><meta charset='UTF-8'></head><body>"
        "<div id='root'></div>"
        f"<script>window.ARTIFACT_DATA = {_json_for_script(data)};{before_runtime}</script>"
        f"{get_inline_scripts('page')}"
        f"{boundary_tag}"
        f"{code}"
        "</body></html>"
    )


# A scripted stub of ArtifactFrame: records every message, answers only when
# the test says so, always to the artifact iframe's window.
STUB_PARENT = """<!DOCTYPE html><html><body>
<script>
  window.__msgs = [];
  window.__requests = [];
  window.addEventListener('message', function (e) {
    var d = e.data;
    if (!d || typeof d !== 'object') return;
    window.__msgs.push(d);
    if (d.type === 'APP_DATA_REQUEST') window.__requests.push(d);
  });
  window.__reply = function (result) {
    var target = document.getElementById('app').contentWindow;
    target.postMessage(Object.assign({ type: 'APP_DATA_RESULT' }, result), '*');
  };
</script>
<iframe id="app" src="child.html" style="width:900px;height:700px"></iframe>
</body></html>"""


class _Session:
    def __init__(self, browser, page, page_errors):
        self.browser = browser
        self.page = page
        self.page_errors = page_errors

    @property
    def child(self):
        for frame in self.page.frames:
            if frame.url.endswith("child.html"):
                return frame
        return self.page.main_frame


async def _launch(playwright):
    import os

    browser = await playwright.chromium.launch(
        headless=True,
        executable_path=os.environ.get("BOW_CHROMIUM_EXECUTABLE") or None,
    )
    page = await browser.new_page(viewport={"width": 1100, "height": 900})
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return _Session(browser, page, page_errors)


async def _open_top_level(playwright, tmp_path, html: str) -> _Session:
    s = await _launch(playwright)
    path = tmp_path / "top.html"
    path.write_text(html, encoding="utf-8")
    await s.page.goto(path.as_uri(), wait_until="load")
    return s


async def _open_in_stub_parent(playwright, tmp_path, child_html: str) -> _Session:
    s = await _launch(playwright)
    (tmp_path / "child.html").write_text(child_html, encoding="utf-8")
    parent = tmp_path / "parent.html"
    parent.write_text(STUB_PARENT, encoding="utf-8")
    await s.page.goto(parent.as_uri(), wait_until="load")
    return s


async def _wait_text(frame, selector: str, expected: str, timeout: int = 10_000):
    await frame.wait_for_function(
        "([sel, want]) => { const el = document.querySelector(sel); return !!el && el.textContent === want; }",
        arg=[selector, expected],
        timeout=timeout,
    )


async def _text(frame, selector: str) -> str:
    return await frame.eval_on_selector(selector, "el => el.textContent")


async def _wait_requests(page, n: int, timeout: int = 10_000):
    await page.wait_for_function(f"() => window.__requests.length >= {n}", timeout=timeout)
    return await page.evaluate("window.__requests")


async def _reply(page, result: dict):
    await page.evaluate("r => window.__reply(r)", result)


def _record(rid_free_id: str, text: str, version: int = 1) -> dict:
    return {
        "id": rid_free_id,
        "data": {"text": text},
        "user": VIEWER,
        "version": version,
        "created_at": "2026-09-27T10:00:00Z",
        "updated_at": "2026-09-27T10:00:00Z",
        "mine": True,
    }


# ── Top-level page: the headless validation / thumbnail / PDF path ─────────────


@requires_browser
@pytest.mark.asyncio
async def test_memory_host_serves_crud_on_a_top_level_page(tmp_path):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        s = await _open_top_level(p, tmp_path, _artifact_page(NOTES_APP))
        try:
            f = s.page.main_frame
            await _wait_text(f, "#loading", "no")
            assert await _text(f, "#count") == "0"
            assert await _text(f, "#error") == "none"

            await s.page.click("#add")
            await _wait_text(f, "#count", "1")
            assert await _text(f, "#count2") == "1", "components sharing a collection must share the store"
            note = s.page.locator("li.note").first
            assert await note.inner_text() == "new note"
            assert await note.get_attribute("data-mine") == "true"
            assert await note.get_attribute("data-user") == VIEWER["name"]
            assert await note.get_attribute("data-version") == "1"

            await s.page.click("#update")
            await _wait_text(f, "li.note", "edited")
            assert await note.get_attribute("data-version") == "2"

            await s.page.click("#remove")
            await _wait_text(f, "#count", "0")
            assert await _text(f, "#error") == "none"
            assert s.page_errors == []
        finally:
            await s.browser.close()


@requires_browser
@pytest.mark.asyncio
async def test_host_hook_wins_and_a_tagged_rejection_is_not_a_page_error(tmp_path):
    """`__bowAppDataHost` is preferred over the memory host; a rejected write
    that generated code never catches is swallowed, not reported."""
    from playwright.async_api import async_playwright

    hook = """
      window.__hookCalls = [];
      window.__bowAppDataHost = function (req) {
        window.__hookCalls.push(req);
        if (req.op === 'list') return Promise.resolve({ rid: req.rid, ok: true, items: [] });
        return Promise.resolve({ rid: req.rid, ok: false, error: { code: 'forbidden', message: 'Not allowed' } });
      };
    """
    async with async_playwright() as p:
        s = await _open_top_level(p, tmp_path, _artifact_page(NOTES_APP, before_runtime=hook))
        try:
            f = s.page.main_frame
            await _wait_text(f, "#loading", "no")
            await s.page.click("#add")
            await _wait_text(f, "#error", "forbidden")
            await s.page.wait_for_timeout(300)

            calls = await s.page.evaluate("window.__hookCalls")
            assert [c["op"] for c in calls] == ["list", "create"]
            assert calls[1]["collection"] == "notes" and calls[1]["data"] == {"text": "new note"}
            assert all(isinstance(c["rid"], str) and c["rid"] for c in calls)
            assert await _text(f, "#count") == "0"
            assert s.page_errors == [], s.page_errors
        finally:
            await s.browser.close()


# ── Iframe under a scripted parent host (ArtifactFrame / public page shape) ────


@requires_browser
@pytest.mark.asyncio
async def test_bridge_correlates_rid_and_ignores_foreign_results(tmp_path):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        s = await _open_in_stub_parent(p, tmp_path, _artifact_page(NOTES_APP, boundary=True))
        try:
            reqs = await _wait_requests(s.page, 1)
            await s.page.wait_for_timeout(300)
            reqs = await s.page.evaluate("window.__requests")
            assert len(reqs) == 1, f"two components on one collection must issue one list: {reqs}"
            req = reqs[0]
            assert req["type"] == "APP_DATA_REQUEST" and req["op"] == "list" and req["collection"] == "notes"
            rid = req["rid"]
            assert isinstance(rid, str) and rid

            child = s.child
            assert await _text(child, "#loading") == "yes"

            # Same rid, wrong source: the iframe posting to itself.
            await child.evaluate(
                "rid => window.postMessage({ type: 'APP_DATA_RESULT', rid, ok: true, items: [{ id: 'forged', data: { text: 'forged' }, version: 1 }] }, '*')",
                rid,
            )
            # Right source, unknown rid.
            await _reply(s.page, {"rid": rid + "-other", "ok": True, "items": [_record("x", "wrong rid")]})
            await s.page.wait_for_timeout(400)
            assert await _text(child, "#count") == "0"
            assert await _text(child, "#loading") == "yes"

            await _reply(s.page, {"rid": rid, "ok": True, "items": [_record("n1", "real note")]})
            await _wait_text(child, "#count", "1")
            assert await _text(child, "li.note") == "real note"
            assert await _text(child, "#count2") == "1"
            assert await _text(child, "#loading") == "no"

            # A late duplicate for an already-settled rid changes nothing.
            await _reply(s.page, {"rid": rid, "ok": True, "items": []})
            await s.page.wait_for_timeout(300)
            assert await _text(child, "#count") == "1"

            await child.click("#add")
            reqs = await _wait_requests(s.page, 2)
            create = reqs[1]
            assert create["op"] == "create" and create["data"] == {"text": "new note"}
            assert create["rid"] != rid
            assert await _text(child, "#loading") == "yes", "a pending write shows loading"
            await _reply(s.page, {"rid": create["rid"], "ok": True, "record": _record("n2", "new note")})
            await _wait_text(child, "#count", "2")
            assert s.page_errors == []
        finally:
            await s.browser.close()


@requires_browser
@pytest.mark.asyncio
async def test_uncaught_write_failure_is_not_an_artifact_error(tmp_path):
    """PP14: the host answers 403 to an uncaught notes.add(); the dashboard
    keeps rendering with error.code, the error boundary posts nothing, and the
    browser reports no page error."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        s = await _open_in_stub_parent(p, tmp_path, _artifact_page(NOTES_APP, boundary=True))
        try:
            reqs = await _wait_requests(s.page, 1)
            await _reply(s.page, {"rid": reqs[0]["rid"], "ok": True, "items": [_record("n1", "kept")]})
            child = s.child
            await _wait_text(child, "#count", "1")

            await child.click("#add")
            reqs = await _wait_requests(s.page, 2)
            await _reply(s.page, {"rid": reqs[1]["rid"], "ok": False,
                                  "error": {"code": "forbidden", "message": "You cannot add notes"}})
            await _wait_text(child, "#error", "forbidden")
            await s.page.wait_for_timeout(500)

            msgs = await s.page.evaluate("window.__msgs")
            assert not [m for m in msgs if m.get("type") == "ARTIFACT_ERROR"], msgs
            assert s.page_errors == [], s.page_errors
            assert await _text(child, "li.note") == "kept", "dashboard must stay visible"

            # The boundary is live: an ordinary uncaught rejection still reports.
            await child.evaluate("() => { Promise.reject(new Error('plain failure')); }")
            await s.page.wait_for_function(
                "() => window.__msgs.some(m => m.type === 'ARTIFACT_ERROR')", timeout=5_000
            )
        finally:
            await s.browser.close()


@requires_browser
@pytest.mark.asyncio
async def test_conflict_sets_error_and_refreshes(tmp_path):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        s = await _open_in_stub_parent(p, tmp_path, _artifact_page(NOTES_APP, boundary=True))
        try:
            reqs = await _wait_requests(s.page, 1)
            await _reply(s.page, {"rid": reqs[0]["rid"], "ok": True, "items": [_record("n1", "mine", 1)]})
            child = s.child
            await _wait_text(child, "#count", "1")

            await child.click("#update")
            reqs = await _wait_requests(s.page, 2)
            upd = reqs[1]
            assert upd["op"] == "update" and upd["id"] == "n1"
            assert upd["version"] == 1 and upd["data"] == {"text": "edited"}
            await _reply(s.page, {"rid": upd["rid"], "ok": False,
                                  "error": {"code": "conflict", "message": "Changed by someone else"}})
            await _wait_text(child, "#error", "conflict")

            reqs = await _wait_requests(s.page, 3)
            assert reqs[2]["op"] == "list" and reqs[2]["collection"] == "notes"
            await _reply(s.page, {"rid": reqs[2]["rid"], "ok": True, "items": [_record("n1", "theirs", 2)]})
            await _wait_text(child, "li.note", "theirs")
            assert await child.get_attribute("li.note", "data-version") == "2"
            assert await _text(child, "#error") == "conflict", "the refresh must not hide why the edit failed"

            msgs = await s.page.evaluate("window.__msgs")
            assert not [m for m in msgs if m.get("type") == "ARTIFACT_ERROR"]
            assert s.page_errors == []
        finally:
            await s.browser.close()


@requires_browser
@pytest.mark.asyncio
async def test_unauthenticated_list_surfaces_as_error_code(tmp_path):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        s = await _open_in_stub_parent(p, tmp_path, _artifact_page(NOTES_APP, boundary=True))
        try:
            reqs = await _wait_requests(s.page, 1)
            await _reply(s.page, {"rid": reqs[0]["rid"], "ok": False,
                                  "error": {"code": "unauthenticated", "message": "Sign in to see notes"}})
            child = s.child
            await _wait_text(child, "#error", "unauthenticated")
            assert await _text(child, "#loading") == "no"
            assert await _text(child, "#count") == "0"
            msgs = await s.page.evaluate("window.__msgs")
            assert not [m for m in msgs if m.get("type") == "ARTIFACT_ERROR"]
            assert s.page_errors == []
        finally:
            await s.browser.close()


@requires_browser
@pytest.mark.asyncio
async def test_unanswered_request_times_out(tmp_path):
    """PP12: a host that never answers ends in `timeout`, not a spinner."""
    from playwright.async_api import async_playwright

    child_html = _artifact_page(
        NOTES_APP, before_runtime="window.__BOW_APP_DATA_TIMEOUT_MS = 300;", boundary=True
    )
    async with async_playwright() as p:
        s = await _open_in_stub_parent(p, tmp_path, child_html)
        try:
            await _wait_requests(s.page, 1)
            child = s.child
            # Far below the 20 s default: proves the configured timeout is used.
            await _wait_text(child, "#error", "timeout", timeout=3_000)
            assert await _text(child, "#loading") == "no"
            assert s.page_errors == []
        finally:
            await s.browser.close()


@requires_browser
@pytest.mark.asyncio
async def test_artifact_without_use_collection_sends_no_request(tmp_path):
    """PP9: legacy artifacts see no new traffic."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        s = await _open_in_stub_parent(p, tmp_path, _artifact_page(PLAIN_APP, boundary=True))
        try:
            child = s.child
            await child.wait_for_selector("#plain", timeout=10_000)
            await s.page.wait_for_timeout(500)
            msgs = await s.page.evaluate("window.__msgs")
            assert not [m for m in msgs if m.get("type") == "APP_DATA_REQUEST"], msgs
            assert not [m for m in msgs if m.get("type") == "ARTIFACT_ERROR"], msgs
        finally:
            await s.browser.close()


# ── Failure paths that must not leave the UI silently out of sync ────────────

# Long enough for the test to answer a request, short enough to watch expire.
SHORT_TIMEOUT = "window.__BOW_APP_DATA_TIMEOUT_MS = 1500;"


async def _open_loaded(p, tmp_path, items):
    """Stub parent with the first list answered."""
    s = await _open_in_stub_parent(
        p, tmp_path, _artifact_page(NOTES_APP, before_runtime=SHORT_TIMEOUT, boundary=True)
    )
    reqs = await _wait_requests(s.page, 1)
    await _reply(s.page, {"rid": reqs[0]["rid"], "ok": True, "items": items})
    await _wait_text(s.child, "#count", str(len(items)))
    return s


async def _ops(page):
    return [r["op"] for r in await page.evaluate("window.__requests")]


async def _settled(child, expr: str) -> str:
    """Run a store call; returns 'resolved' or '<code>|<tagged>'."""
    return await child.evaluate(
        f"() => ({expr}).then(() => 'resolved', e => e.code + '|' + (e.__bowAppDataError === true))"
    )


@requires_browser
@pytest.mark.asyncio
async def test_failed_first_list_stays_visible_and_is_retried_after_a_write(tmp_path):
    """H1: a successful write must not hide that the list never loaded."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        s = await _open_in_stub_parent(
            p, tmp_path, _artifact_page(NOTES_APP, before_runtime=SHORT_TIMEOUT, boundary=True)
        )
        try:
            child = s.child
            await _wait_requests(s.page, 1)
            await _wait_text(child, "#error", "timeout", timeout=5_000)

            await child.click("#add")
            reqs = await _wait_requests(s.page, 2)
            assert reqs[1]["op"] == "create"
            await _reply(s.page, {"rid": reqs[1]["rid"], "ok": True, "record": _record("n2", "new note")})
            await _wait_text(child, "#count", "1")
            assert await _text(child, "#error") == "timeout", "the list failure must stay visible"

            reqs = await _wait_requests(s.page, 3)
            assert reqs[2]["op"] == "list", "the failed list must be retried"
            await _reply(s.page, {"rid": reqs[2]["rid"], "ok": True,
                                  "items": [_record("n1", "old note"), _record("n2", "new note")]})
            await _wait_text(child, "#count", "2")
            assert await _text(child, "#error") == "none"
            assert s.page_errors == []
        finally:
            await s.browser.close()


@requires_browser
@pytest.mark.asyncio
async def test_failed_first_list_is_retried_on_remount(tmp_path):
    """H1: mounting a component (ensureLoaded) after a failed list re-lists."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        s = await _open_in_stub_parent(
            p, tmp_path, _artifact_page(NOTES_APP, before_runtime=SHORT_TIMEOUT, boundary=True)
        )
        try:
            child = s.child
            await _wait_requests(s.page, 1)
            await _wait_text(child, "#error", "timeout", timeout=5_000)
            # What useCollection's mount effect calls.
            await child.evaluate("() => window.__appDataStore.get('notes').ensureLoaded()")
            reqs = await _wait_requests(s.page, 2)
            assert reqs[1]["op"] == "list"
            await _reply(s.page, {"rid": reqs[1]["rid"], "ok": True, "items": [_record("n1", "loaded")]})
            await _wait_text(child, "#count", "1")
            assert await _text(child, "#error") == "none"
        finally:
            await s.browser.close()


@requires_browser
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        "window.__notes.add({ text: 'x', n: NaN })",
        "window.__notes.add({ n: Infinity })",
        "window.__notes.add({ text: 'x', f: function () {} })",
        "window.__notes.add({ text: undefined })",
        "window.__notes.add({ snapshot: { rows: [1, -Infinity] } })",
        "window.__notes.update('n1', { text: NaN })",
    ],
    ids=["nan", "infinity", "function", "undefined", "nested-infinity", "update-nan"],
)
async def test_non_json_values_are_rejected_before_sending(tmp_path, call):
    """H2: JSON would silently turn these into null; fail locally instead."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        s = await _open_loaded(p, tmp_path, [_record("n1", "kept")])
        try:
            child = s.child
            assert await _settled(child, call) == "validation|true"
            await _wait_text(child, "#error", "validation")
            await s.page.wait_for_timeout(300)
            assert await _ops(s.page) == ["list"], "nothing may be sent"
            assert await _text(child, "li.note") == "kept"
            assert s.page_errors == []
        finally:
            await s.browser.close()


@requires_browser
@pytest.mark.asyncio
@pytest.mark.parametrize("button,op", [("add", "create"), ("update", "update"), ("remove", "delete")])
async def test_write_timeout_refreshes_the_list(tmp_path, button, op):
    """M1: the write may have landed; re-read instead of guessing."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        s = await _open_loaded(p, tmp_path, [_record("n1", "mine")])
        try:
            child = s.child
            await child.click(f"#{button}")
            await _wait_requests(s.page, 2)
            await _wait_text(child, "#error", "timeout", timeout=5_000)
            await s.page.wait_for_timeout(500)
            assert await _ops(s.page) == ["list", op, "list"]
            reqs = await s.page.evaluate("window.__requests")
            await _reply(s.page, {"rid": reqs[2]["rid"], "ok": True, "items": [_record("n1", "server copy", 2)]})
            await _wait_text(child, "li.note", "server copy")
            assert await _text(child, "#error") == "timeout", "the refresh must not hide why the write failed"
            assert s.page_errors == []
        finally:
            await s.browser.close()


@requires_browser
@pytest.mark.asyncio
async def test_malformed_list_response_is_an_error(tmp_path):
    """M4: ok:true without items is not an empty collection."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        s = await _open_in_stub_parent(p, tmp_path, _artifact_page(NOTES_APP, boundary=True))
        try:
            child = s.child
            reqs = await _wait_requests(s.page, 1)
            await _reply(s.page, {"rid": reqs[0]["rid"], "ok": True})
            await _wait_text(child, "#error", "error")
            assert await child.evaluate("() => window.__notes.error.message") == "Malformed app data response"
            assert await _text(child, "#loading") == "no"
            assert s.page_errors == []
        finally:
            await s.browser.close()


@requires_browser
@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["window.__notes.add({ text: 'x' })", "window.__notes.update('n1', { text: 'y' })"],
                         ids=["create", "update"])
async def test_malformed_write_response_is_an_error(tmp_path, call):
    """M4: ok:true without a record must not resolve with undefined."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        s = await _open_loaded(p, tmp_path, [_record("n1", "kept")])
        try:
            child = s.child
            await child.evaluate(
                f"() => {{ window.__outcome = null; ({call}).then(r => {{ window.__outcome = 'resolved:' + typeof r; }},"
                " e => { window.__outcome = e.code; }); }"
            )
            reqs = await _wait_requests(s.page, 2)
            await _reply(s.page, {"rid": reqs[1]["rid"], "ok": True})
            await child.wait_for_function("() => window.__outcome !== null", timeout=5_000)
            assert await child.evaluate("() => window.__outcome") == "error"
            await _wait_text(child, "#error", "error")
            assert await child.evaluate("() => window.__notes.error.message") == "Malformed app data response"
            assert await _text(child, "li.note") == "kept"
            assert s.page_errors == []
        finally:
            await s.browser.close()


@requires_browser
@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["window.__notes.update('ghost', { text: 'x' })", "window.__notes.remove('ghost')"],
                         ids=["update", "remove"])
async def test_unknown_id_fails_locally_as_not_found_and_refreshes(tmp_path, call):
    """M6: never send version undefined for a record the store does not hold."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        s = await _open_loaded(p, tmp_path, [_record("n1", "kept")])
        try:
            child = s.child
            assert await _settled(child, call) == "not_found|true"
            await _wait_text(child, "#error", "not_found")
            await s.page.wait_for_timeout(300)
            assert await _ops(s.page) == ["list", "list"], "no write for an unknown id, then a refresh"
            assert s.page_errors == []
        finally:
            await s.browser.close()


@requires_browser
@pytest.mark.asyncio
async def test_conflict_error_survives_a_reused_in_flight_list(tmp_path):
    """M7: a refresh already in flight is reused after a conflict; its
    success must not clear the conflict error."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        s = await _open_loaded(p, tmp_path, [_record("n1", "mine", 1)])
        try:
            child = s.child
            await child.evaluate("() => { window.__notes.refresh(); }")
            reqs = await _wait_requests(s.page, 2)
            assert reqs[1]["op"] == "list"

            await child.click("#update")
            reqs = await _wait_requests(s.page, 3)
            assert reqs[2]["op"] == "update"
            await _reply(s.page, {"rid": reqs[2]["rid"], "ok": False,
                                  "error": {"code": "conflict", "message": "Changed by someone else"}})
            await _wait_text(child, "#error", "conflict")

            await _reply(s.page, {"rid": reqs[1]["rid"], "ok": True, "items": [_record("n1", "theirs", 2)]})
            await _wait_text(child, "li.note", "theirs")
            assert await _text(child, "#error") == "conflict"
            assert s.page_errors == []
        finally:
            await s.browser.close()


# ── Hosts without a server ─────────────────────────────────────────────────────


@requires_browser
@pytest.mark.asyncio
async def test_html_export_answers_unavailable(tmp_path):
    from playwright.async_api import async_playwright

    from app.services.artifact_libs import (
        get_export_vendor_scripts,
        get_globals_script,
        get_offline_host_script,
    )

    data = {"visualizations": [], "current_user": None, "runtime": {"version": 11}}
    html = (
        "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
        f"{get_export_vendor_scripts()}</head><body><div id='root'></div>"
        f"<script>window.ARTIFACT_DATA = {_json_for_script(data)};</script>"
        f"{get_globals_script()}{get_offline_host_script()}{EXPORT_APP_JS}"
        "</body></html>"
    )
    async with async_playwright() as p:
        s = await _open_top_level(p, tmp_path, html)
        try:
            f = s.page.main_frame
            await _wait_text(f, "#error", "unavailable", timeout=5_000)
            assert await _text(f, "#loading") == "no"
            assert await _text(f, "#count") == "0"
            assert s.page_errors == []
        finally:
            await s.browser.close()


@requires_browser
@pytest.mark.asyncio
async def test_mcp_app_installs_an_unavailable_host():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        s = await _launch(p)
        try:
            await s.page.set_content(MCP_HTML.read_text(encoding="utf-8"))
            result = await s.page.evaluate(
                "() => window.__bowAppDataHost({ rid: 'r1', op: 'list', collection: 'notes' })"
            )
            assert result["ok"] is False
            assert result["error"]["code"] == "unavailable"
            assert result["rid"] == "r1"
        finally:
            await s.browser.close()

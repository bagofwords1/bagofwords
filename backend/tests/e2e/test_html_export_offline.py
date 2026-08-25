"""The "Export dashboard as HTML" promise, enforced against a real browser.

The feature makes one hard claim: the downloaded file renders completely with
no server, no session and ZERO network requests. That claim cannot be checked
by reading the HTML — the runtime reaches for the network in places that are
easy to miss (pdf.js pins an absolute /libs/ worker path, generated markup
sometimes carries a Google Fonts <link>, param controls post to a host that no
longer exists). So these tests open the actual export in Chromium with every
http(s) request aborted and recorded, and fail if anything was requested.

They also pin the offline params contract (Tier 2 in
frontend/public/libs/artifact-offline-host.js):

  * a param that was UNFILTERED at export time filters the embedded rows
    locally, so a control the user can operate keeps working offline;
  * a param whose value was already baked into the snapshot stays inert and is
    named in __BOW_OFFLINE_EXPORT.unemulated, because re-filtering rows the
    server already narrowed would present a wrong number as a fact.

Needs Playwright plus the vendored JS libs (gitignored, fetched at Docker build
time by scripts/download-vendor-libs.sh) and skips when either is absent.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      python -m pytest tests/e2e/test_html_export_offline.py -v -s
"""
import os
import re
import uuid
from pathlib import Path

import pytest

from app.dependencies import async_session_maker
from app.models.artifact import Artifact
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.user import User
from app.models.visualization import Visualization
from app.models.widget import Widget
from app.services.html_export_service import (
    ExportUnavailable,
    build_standalone_html,
)

pytestmark = pytest.mark.e2e


def _browser_stack_available() -> bool:
    """Playwright package + a downloaded Chromium + the vendored export libs.

    The libs directory exists in a checkout but holds only artifact-globals.js,
    so the individual files have to be checked.
    """
    # The SYNC api on purpose: this runs at module import, outside any event
    # loop, to decide a skipif. The tests themselves use the async api because
    # the sync one refuses to run inside a live loop.
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False

    from app.services.artifact_libs import (
        _CANDIDATE_DIRS,
        _EXPORT_LIBS,
        _GLOBALS_FILENAME,
        _OFFLINE_HOST_FILENAME,
        _find_libs_dir,
    )

    libs_dir = _find_libs_dir()
    if libs_dir is None:
        return False
    if not all((libs_dir / name).is_file() for name in (*_EXPORT_LIBS, _GLOBALS_FILENAME)):
        return False
    # The offline host is checked across every candidate dir, matching
    # get_offline_host_script: _find_libs_dir ranks by artifact-globals.js
    # mtime, so a stale frontend/.output could win and silently skip the very
    # tests that guard this feature.
    if not any((d / _OFFLINE_HOST_FILENAME).is_file() for d in _CANDIDATE_DIRS):
        return False

    # Same override the artifact screenshot and transpile paths honour: a
    # deployment (or a dev machine) may have a compatible Chromium without the
    # Playwright-managed download.
    override = os.environ.get("BOW_CHROMIUM_EXECUTABLE")
    if override:
        return Path(override).exists()

    try:
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


# Evaluated ONCE, at import, while no event loop is running. Re-calling it from
# inside an async test would hit the sync-API-in-asyncio guard, be swallowed by
# the except, and silently report "no browser".
BROWSER_AVAILABLE = _browser_stack_available()

requires_browser = pytest.mark.skipif(
    not BROWSER_AVAILABLE,
    reason="needs Playwright + a downloaded Chromium + the vendored JS libs "
           "(scripts/download-vendor-libs.sh)",
)


def _export_libs_available() -> bool:
    """Whether build_standalone_html can read every lib it might reach for.

    `_find_libs_dir` only proves that a candidate directory exists and is
    non-empty, which a fresh checkout always satisfies: .gitignore, .gitkeep
    and the two committed helper scripts live there while the downloaded libs
    do not. babel-standalone is in the list even though _EXPORT_LIBS omits it,
    because the export falls back to inlining babel whenever server-side
    transpilation does not apply — which is the path these tests hit.
    """
    from app.services.artifact_libs import (
        _EXPORT_LIBS,
        _GLOBALS_FILENAME,
        _find_libs_dir,
    )

    libs_dir = _find_libs_dir()
    if libs_dir is None:
        return False
    needed = (*_EXPORT_LIBS, _GLOBALS_FILENAME, "babel-standalone.min.js")
    return all((libs_dir / name).is_file() for name in needed)


EXPORT_LIBS_AVAILABLE = _export_libs_available()

requires_export_libs = pytest.mark.skipif(
    not EXPORT_LIBS_AVAILABLE,
    reason="needs the vendored JS libs (scripts/download-vendor-libs.sh)",
)

REGIONS = ["US", "US", "FR", "FR", "FR"]

# Exercises the three things an export has to carry: the data runtime
# (useArtifactData), a real chart (EChart -> canvas), and a param control
# (useParams) whose commit would hang without the offline host.
DASHBOARD_CODE = """<script type="text/babel">
function App() {
  const data = useArtifactData();
  const { values, setParams, apply, loading } = useParams();
  if (!data) return <div id="pending">loading</div>;
  const sales = data.visualizations[0];
  const rows = sales.rows || [];
  return (
    <div className="w-full min-h-full p-6">
      <h1 className="text-2xl font-bold">Regional sales</h1>
      <div id="rowcount">{rows.length}</div>
      <div id="spinner">{loading ? 'busy' : 'idle'}</div>
      <select
        id="region"
        value={values.region || ''}
        onChange={e => { setParams({ region: e.target.value }, { apply: false }); apply(); }}
      >
        <option value="">All</option>
        <option value="US">US</option>
        <option value="FR">FR</option>
      </select>
      <EChart option={{
        xAxis: { type: 'category', data: rows.map(r => r.region) },
        yAxis: { type: 'value' },
        series: [{ type: 'bar', data: rows.map(r => r.amount) }]
      }} style={{ height: 240 }} />
      <p>TAIL_OF_DASHBOARD</p>
    </div>
  );
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>"""


async def _seed(
    code: str = DASHBOARD_CODE,
    mode: str = "page",
    parameters=None,
    artifact_visibility: str | None = None,
):
    """A report with one visualization whose query may declare params."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"HTML Export Org {suffix}")
        db.add(org)
        await db.flush()

        user = User(
            name="Dashboard Owner",
            email=f"html-export-{suffix}@example.com",
            hashed_password="x",
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        db.add(user)
        await db.flush()

        report = Report(
            title="Regional Sales",
            slug=f"regional-{suffix}",
            organization_id=org.id,
            user_id=user.id,
            **({"artifact_visibility": artifact_visibility} if artifact_visibility else {}),
        )
        db.add(report)
        await db.flush()

        widget = Widget(title="Sales", slug=f"w-{suffix}", report_id=report.id)
        db.add(widget)
        await db.flush()

        step = Step(
            title="Sales step", slug=f"s-{suffix}", status="success",
            widget_id=widget.id,
            data={
                "rows": [
                    {"region": r, "amount": (i + 1) * 10}
                    for i, r in enumerate(REGIONS)
                ],
                "columns": [{"field": "region"}, {"field": "amount"}],
            },
            data_model={"type": "table"},
        )
        db.add(step)
        await db.flush()

        query = Query(
            title="Sales", report_id=report.id, widget_id=widget.id,
            default_step_id=step.id, parameters=parameters or [],
        )
        db.add(query)
        await db.flush()
        step.query_id = query.id
        await db.flush()

        viz = Visualization(
            title="Sales", status="success", report_id=report.id,
            query_id=query.id, view={"type": "bar_chart"},
        )
        db.add(viz)
        await db.flush()

        artifact = Artifact(
            report_id=report.id, user_id=user.id, organization_id=org.id,
            title="Regional Dashboard", mode=mode,
            content={"code": code, "visualization_ids": [str(viz.id)]},
        )
        db.add(artifact)
        await db.flush()
        await db.commit()
        return str(report.id), str(artifact.id)


async def _export(artifact_id: str) -> str:
    async with async_session_maker() as db:
        artifact = await db.get(Artifact, artifact_id)
        return await build_standalone_html(db, artifact)


class OfflinePage:
    """A rendered export plus everything the browser tried to reach.

    Requests are both aborted (so a leak cannot accidentally succeed on a
    machine with connectivity) and recorded (so the test can name what leaked).
    """

    def __init__(self, page, blocked, requests, console_errors):
        self.page = page
        self.blocked = blocked
        self.requests = requests
        self.console_errors = console_errors


async def _open_offline(playwright, html: str, tmp_path: Path):
    """Open the export in Chromium with every remote request denied.

    The async Playwright API is required here: the sync one refuses to run
    inside a live asyncio loop, which is exactly where these tests execute.
    """
    path = tmp_path / "export.html"
    path.write_text(html, encoding="utf-8")

    browser = await playwright.chromium.launch(
        headless=True,
        executable_path=os.environ.get("BOW_CHROMIUM_EXECUTABLE") or None,
    )
    page = await browser.new_page(viewport={"width": 1280, "height": 900})

    blocked: list[str] = []
    requests: list[str] = []
    console_errors: list[str] = []

    async def _block(route):
        blocked.append(route.request.url)
        await route.abort()

    # http/https patterns rather than "**/*": Playwright does not intercept
    # file:// navigation, and matching only remote schemes keeps the intent
    # unambiguous — anything caught here is a genuine network reference.
    await page.route("http://**", _block)
    await page.route("https://**", _block)

    page.on("request", lambda r: requests.append(r.url))
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: console_errors.append(str(e)))

    await page.goto(path.as_uri(), wait_until="load")
    await page.wait_for_function(
        "() => { const r = document.getElementById('root'); return r && r.children.length > 0; }",
        timeout=20_000,
    )
    return browser, OfflinePage(page, blocked, requests, console_errors)


def _remote(urls):
    return [u for u in urls if u.startswith("http://") or u.startswith("https://")]


@requires_browser
@pytest.mark.asyncio
async def test_export_renders_with_zero_network_requests(tmp_path):
    """The whole promise: it renders, fully, having asked the network for nothing."""
    from playwright.async_api import async_playwright

    _, artifact_id = await _seed()
    html = await _export(artifact_id)

    async with async_playwright() as p:
        browser, ctx = await _open_offline(p, html, tmp_path)
        try:
            assert not ctx.blocked, f"export requested remote resources: {ctx.blocked}"
            assert not _remote(ctx.requests), \
                f"export requested remote resources: {_remote(ctx.requests)}"

            body = await ctx.page.inner_text("body")
            assert "Regional sales" in body
            assert "TAIL_OF_DASHBOARD" in body, "the dashboard did not render to the end"

            # ECharts actually drew: a canvas with real pixels, not an empty box.
            assert await ctx.page.locator("#root canvas").count() > 0, "no chart canvas rendered"
            box = await ctx.page.locator("#root canvas").first.bounding_box()
            assert box and box["width"] > 50 and box["height"] > 50, f"chart not laid out: {box}"

            # Tailwind's JIT ran offline, so utility classes resolved to styles.
            font_weight = await ctx.page.eval_on_selector(
                "h1", "el => getComputedStyle(el).fontWeight"
            )
            assert int(font_weight) >= 700, f"Tailwind did not apply (font-weight {font_weight})"

            assert not ctx.console_errors, f"console errors in export: {ctx.console_errors}"
        finally:
            await browser.close()


@requires_browser
@pytest.mark.asyncio
async def test_unfiltered_param_filters_rows_locally(tmp_path):
    """A param with no value baked into the snapshot keeps working offline.

    Without the offline host this is the case that hangs: the control commits,
    `post()` reaches nobody, and `loading` never clears.
    """
    from playwright.async_api import async_playwright

    _, artifact_id = await _seed(parameters=[{"name": "region", "type": "string"}])
    html = await _export(artifact_id)

    async with async_playwright() as p:
        browser, ctx = await _open_offline(p, html, tmp_path)
        try:
            assert await ctx.page.inner_text("#rowcount") == str(len(REGIONS))

            await ctx.page.select_option("#region", "FR")
            await ctx.page.wait_for_function(
                "() => document.getElementById('rowcount').textContent === '3'",
                timeout=10_000,
            )
            # The spinner must clear — the whole point of answering the commit.
            assert await ctx.page.inner_text("#spinner") == "idle"

            # Widening recovers rows, proving filtering derives from the
            # baseline rather than compounding on the previous result.
            await ctx.page.select_option("#region", "")
            await ctx.page.wait_for_function(
                f"() => document.getElementById('rowcount').textContent === '{len(REGIONS)}'",
                timeout=10_000,
            )

            assert not ctx.blocked, f"filtering hit the network: {ctx.blocked}"
            emulated = await ctx.page.evaluate("() => window.__BOW_OFFLINE_EXPORT.emulated")
            assert "region" in emulated
        finally:
            await browser.close()


@requires_browser
@pytest.mark.asyncio
async def test_param_baked_into_the_snapshot_is_reported_inert(tmp_path):
    """A param the server already applied must not be re-filtered locally.

    The embedded rows are only the matching slice, so filtering them to another
    value would quietly show an empty or partial result as if it were real.
    """
    from playwright.async_api import async_playwright

    _, artifact_id = await _seed(
        parameters=[{"name": "region", "type": "string", "default": "US"}]
    )
    html = await _export(artifact_id)

    async with async_playwright() as p:
        browser, ctx = await _open_offline(p, html, tmp_path)
        try:
            state = await ctx.page.evaluate("() => window.__BOW_OFFLINE_EXPORT")
            assert "region" in state["unemulated"], state
            assert state["reasons"]["region"] == "baked-in"
            assert "region" not in state["emulated"]

            before = await ctx.page.inner_text("#rowcount")
            await ctx.page.select_option("#region", "FR")
            # It must answer (no hang) but must not invent a filtered result.
            await ctx.page.wait_for_function(
                "() => document.getElementById('spinner').textContent === 'idle'",
                timeout=10_000,
            )
            assert await ctx.page.inner_text("#rowcount") == before, \
                "an inert param filtered the snapshot anyway"

            badge = ctx.page.locator("[data-bow-export-badge]")
            assert await badge.count() == 1
            assert "region" in await badge.inner_text()
        finally:
            await browser.close()


@requires_export_libs
@pytest.mark.asyncio
async def test_no_external_subresource_tags_are_emitted():
    """Nothing is *linked*; everything is inlined.

    A cheap structural guard that runs without a browser, so a regression that
    reintroduces `<script src="/libs/...">` fails even in a CI job that skips
    the rendering tests.
    """

    _, artifact_id = await _seed()
    try:
        html = await _export(artifact_id)
    except ExportUnavailable as e:
        pytest.skip(str(e))

    assert not re.search(r"<script\b[^>]*\bsrc=", html, re.I), "export links a script"
    assert not re.search(r"<link\b[^>]*\bhref=", html, re.I), "export links a stylesheet"

    if BROWSER_AVAILABLE:
        # Dropping babel-standalone is the whole reason for pre-transpiling.
        assert "text/babel" not in html, "artifact code was not pre-transpiled"
    else:
        # Documented fallback: without a usable headless browser the export
        # still has to work, so it ships Babel inline and compiles in-page.
        assert "text/babel" in html
        assert "@babel/standalone" in html or "babel" in html.lower()


def test_pdf_worker_is_pinned_to_an_inlined_blob():
    """pdf.js is the one library with an absolute URL baked into our runtime.

    artifact-globals.js assigns `workerSrc = '/libs/pdf.worker.min.js'` while
    rendering a <BowFile>, which resolves to nothing in a downloaded file. The
    export inlines the worker and defines workerSrc as a getter, because that
    later assignment would overwrite a plain one.
    """
    from app.services.artifact_libs import _PDF_WORKER_LIB, _find_libs_dir
    from app.services.html_export_service import _pdf_worker_script

    libs_dir = _find_libs_dir()
    if libs_dir is None or not (libs_dir / _PDF_WORKER_LIB).is_file():
        pytest.skip("vendored pdf.js worker not present")

    script = _pdf_worker_script()
    assert "createObjectURL" in script
    assert "Object.defineProperty" in script
    assert "workerSrc" in script
    # A getter, not an assignment — an assignment loses the race with the one
    # inside artifact-globals.js.
    assert "get: function()" in script


@requires_export_libs
@pytest.mark.asyncio
async def test_remote_references_in_generated_code_are_stripped():
    """Generated markup occasionally carries a webfont or a remote image.

    Those are the one thing that can smuggle a network request past every other
    guard, because they live in LLM output rather than in our own templates.
    """

    leaky = """<link href="https://fonts.googleapis.com/css2?family=Inter" rel="stylesheet">
<script type="text/babel">
function App() {
  return <div><img src="https://example.com/logo.png" alt="Logo" />ok</div>;
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>"""

    _, artifact_id = await _seed(code=leaky)
    try:
        html = await _export(artifact_id)
    except ExportUnavailable as e:
        pytest.skip(str(e))

    assert "fonts.googleapis.com" not in html
    assert "https://example.com/logo.png" not in html
    # The <img> keeps its place in the layout; only the remote source goes.
    assert 'alt="Logo"' in html or "alt: \"Logo\"" in html


@pytest.mark.asyncio
async def test_non_page_artifacts_are_refused():
    """Slides and docs have no React runtime to stand up; refuse rather than
    emit a blank page."""
    _, artifact_id = await _seed(mode="slides")
    with pytest.raises(ExportUnavailable, match="page-mode"):
        await _export(artifact_id)


@pytest.mark.parametrize(
    "title,expected_name,expected_ascii",
    [
        ("Regional Sales", "Regional-Sales.html", "Regional-Sales.html"),
        ("Q3 / Revenue (final)", "Q3-Revenue-final.html", "Q3-Revenue-final.html"),
        # RTL and Cyrillic titles keep their real name in the RFC 5987 field and
        # must not degrade to bare punctuation in the ASCII fallback.
        ("דשבורד מכירות", "דשבורד-מכירות.html", "dashboard.html"),
        ("Отчёт", "Отчёт.html", "dashboard.html"),
        ("   ", "dashboard.html", "dashboard.html"),
        (None, "dashboard.html", "dashboard.html"),
    ],
)
def test_download_filenames_survive_non_latin_titles(title, expected_name, expected_ascii):
    """The plain `filename=` must stay a usable ASCII name.

    Transliterating by dropping non-ASCII bytes turns "דשבורד-מכירות.html" into
    "-.html", which a browser saves as a hidden file with no extension.
    """
    from types import SimpleNamespace

    from app.services.html_export_service import (
        ascii_fallback_filename,
        suggested_filename,
    )

    name = suggested_filename(SimpleNamespace(title=title))
    assert name == expected_name
    assert ascii_fallback_filename(name) == expected_ascii


# ── the public /r/{id} page ────────────────────────────────────────────────────
# The shared-dashboard page serves anonymous viewers, so its export route has
# its own gate rather than the app's RBAC decorator. These pin that the gate is
# actually applied — an export that leaked past it would hand a full data
# snapshot to someone who could not open the dashboard in the first place.

async def _export_public(report_id: str, artifact_id=None, user=None):
    from starlette.requests import Request

    from app.routes.report import export_public_report_html

    # The route takes the raw Request only to enrich best-effort audit logging;
    # a minimal ASGI scope stands in for it here.
    request = Request({
        "type": "http", "method": "GET", "path": "/", "headers": [],
        "query_string": b"", "client": ("127.0.0.1", 0),
    })
    async with async_session_maker() as db:
        return await export_public_report_html(
            report_id=report_id, request=request, artifact_id=artifact_id, db=db, user=user,
        )


@requires_export_libs
@pytest.mark.asyncio
async def test_public_export_serves_a_publicly_visible_dashboard():

    report_id, artifact_id = await _seed(artifact_visibility="public")
    response = await _export_public(report_id)

    assert response.status_code == 200
    assert response.media_type == "text/html; charset=utf-8"
    assert response.headers["Cache-Control"] == "no-store"
    assert "attachment" in response.headers["Content-Disposition"]
    body = response.body.decode("utf-8")
    assert "TAIL_OF_DASHBOARD" in body
    # Same guarantee as the in-app export: nothing is linked.
    assert not re.search(r"<script\b[^>]*\bsrc=", body, re.I)

    # Pinning to the on-screen artifact resolves to the same document.
    pinned = await _export_public(report_id, artifact_id=artifact_id)
    assert pinned.status_code == 200


@pytest.mark.asyncio
async def test_public_export_is_refused_when_the_dashboard_is_not_shared():
    """artifact_visibility='none' is owner-only; an anonymous viewer gets 404."""
    from fastapi import HTTPException

    report_id, _ = await _seed()  # defaults to not-shared
    with pytest.raises(HTTPException) as exc:
        await _export_public(report_id)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_public_export_rejects_an_artifact_from_another_report():
    """artifact_id is request input, so it is checked against what this viewer
    was just listed — never trusted to belong to the report in the path."""
    from fastapi import HTTPException

    report_id, _ = await _seed(artifact_visibility="public")
    _, other_artifact_id = await _seed(artifact_visibility="public")

    with pytest.raises(HTTPException) as exc:
        await _export_public(report_id, artifact_id=other_artifact_id)
    assert exc.value.status_code == 404

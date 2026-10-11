"""Server-side headless Chromium (thumbnails, PDF export, agent browser tool)
never fetches anything but the app's own origin — not directly, not through a
redirect, not after DNS changes.

Real Chromium against real local servers. A server on 127.0.0.2 stands in for
the metadata service / an internal host: Chromium can reach it, so "its
request log stays empty" is the proof the guard held. Skipped when no
Chromium is installed.
"""
from __future__ import annotations

import asyncio
import http.server
import threading
from contextlib import contextmanager

import pytest

from app.core import egress


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="Chromium not installed")


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(egress, "block_private_outbound", lambda: False)
    from app.services.report_pdf_service import ReportPdfService
    from app.services.thumbnail_service import ThumbnailService

    monkeypatch.setattr(ReportPdfService, "UPLOADS_DIR", tmp_path)
    monkeypatch.setattr(ThumbnailService, "UPLOADS_DIR", tmp_path)


@contextmanager
def _server(bind="127.0.0.1", routes=None):
    """Yield (origin, seen). routes: path -> (status, headers, body)."""
    seen: list[str] = []
    routes = routes if routes is not None else {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            status, headers, body = routes.get(self.path.split("?")[0], (200, {"Content-Type": "image/gif"}, b"GIF89a"))
            if callable(body):
                body = body()
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer((bind, 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://{bind}:{srv.server_address[1]}", seen
    finally:
        srv.shutdown()
        srv.server_close()


def _page(body: str) -> str:
    return f"<!doctype html><html><body><div id='root'><h1>Revenue by month</h1>{body}</div></body></html>"


def _leaky(target: str) -> str:
    return (f"<img src='{target}/img'><link rel='stylesheet' href='{target}/css'>"
            f"<script>fetch('{target}/fetch').catch(()=>0); new Image().src='{target}/beacon';</script>")


# ── finding 4: thumbnails ──────────────────────────────────────────────────


def test_thumbnail_render_fetches_nothing_from_the_network():
    from app.services.thumbnail_service import ThumbnailService

    with _server("127.0.0.2") as (internal, seen):
        out = asyncio.run(ThumbnailService().generate_thumbnail("t1", _page(_leaky(internal))))
    assert out == "thumbnails/t1.png"  # the render itself still succeeds
    assert seen == []


# ── finding 5: PDF export of page artifacts ────────────────────────────────


@pytest.fixture
def public_host(monkeypatch):
    """Let the 127.0.0.1 server stand in for a public, non-allowlisted host
    (on the old code path a public host was allowed)."""
    import app.services.report_pdf_service as rps

    monkeypatch.setattr(rps, "host_is_public", lambda h: h == "127.0.0.1", raising=False)


def test_page_pdf_does_not_load_external_hosts_or_follow_redirects(public_host):
    from app.services.report_pdf_service import ReportPdfService

    with _server("127.0.0.2") as (internal, internal_seen):
        routes = {"/redirect": (302, {"Location": f"{internal}/latest/meta-data"}, b"")}
        with _server("127.0.0.1", routes) as (external, external_seen):
            html = _page(_leaky(external) + f"<img src='{external}/redirect'>")
            out = asyncio.run(ReportPdfService().generate_pdf("p1", html, mode="page"))
    assert out == "pdfs/p1.pdf"
    assert external_seen == [] and internal_seen == []


def test_page_pdf_of_a_normal_report_still_renders():
    """Inline content only (the vendored libraries are inlined the same way)."""
    from app.services.report_pdf_service import ReportPdfService

    chart = ("<svg width='400' height='200'><rect x='10' y='50' width='40' height='150' fill='#36c'/>"
             "<rect x='60' y='20' width='40' height='180' fill='#36c'/></svg>"
             "<canvas id='c' width='200' height='100'></canvas>"
             "<script>document.getElementById('c').getContext('2d').fillRect(0,0,100,50)</script>"
             "<img src='data:image/gif;base64,R0lGODlhAQABAAAAACw='>")
    out = asyncio.run(ReportPdfService().generate_pdf("p2", _page(chart), mode="page"))
    assert out == "pdfs/p2.pdf"
    assert ReportPdfService.UPLOADS_DIR.joinpath("p2.pdf").stat().st_size > 1000


# ── document PDF: the app's own origin works, its redirects do not escape ──


_DOC_PAGE = """<!doctype html><html><body><h1>Doc</h1>
<img src="/asset.gif"><img src="/redirect">
<script>document.documentElement.setAttribute('data-bow-doc-ready','1')</script>
</body></html>"""


@pytest.mark.parametrize("block_private", [False, True])
def test_doc_pdf_loads_own_origin_but_not_redirect_targets(monkeypatch, block_private):
    from app.services.report_pdf_service import ReportPdfService

    # The render origin is deployment config (loopback in the standard image):
    # it must keep working even with block_private_outbound on.
    monkeypatch.setattr(egress, "block_private_outbound", lambda: block_private)
    with _server("127.0.0.2") as (internal, internal_seen):
        routes = {
            "/print/doc": (200, {"Content-Type": "text/html"}, _DOC_PAGE.encode()),
            "/redirect": (302, {"Location": f"{internal}/latest/meta-data"}, b""),
        }
        with _server("127.0.0.1", routes) as (origin, origin_seen):
            monkeypatch.setenv("BOW_RENDER_ORIGIN", origin)
            out = asyncio.run(ReportPdfService().generate_doc_pdf("d1", {"title": "Doc", "markdown": "# x"}))
    assert out == "pdfs/d1.pdf"
    assert "/print/doc" in origin_seen and "/asset.gif" in origin_seen
    assert internal_seen == []


# ── shared helpers: proxy decision + browser tool policy ───────────────────


def test_renderer_policy_allows_only_own_origin():
    from app.core.render_guard import RendererPolicy
    from app.core.url_safety import UnsafeURLError

    pol = RendererPolicy(["http://127.0.0.1:3000"])
    assert pol.allows_url("http://127.0.0.1:3000/_nuxt/app.js")
    for url in ("http://127.0.0.1:3001/", "https://cdn.jsdelivr.net/npm/x.js", "http://169.254.169.254/",
                "file:///etc/passwd", "ftp://127.0.0.1:3000/"):
        assert not pol.allows_url(url), url
    assert str(pol.decide("127.0.0.1", 3000)) == "127.0.0.1"
    with pytest.raises(UnsafeURLError):
        pol.decide("169.254.169.254", 80)


def test_browser_tool_egress_policy(monkeypatch):
    from types import SimpleNamespace

    from app.ai.tools.implementations._browser_common import browser_egress_decider, host_allowlisted
    from app.core.url_safety import UnsafeURLError
    from tests.utils.egress_fakes import FakeDNS

    monkeypatch.setattr(egress, "_getaddrinfo", FakeDNS({
        "intranet.corp.invalid": "10.0.0.8", "evil.corp.invalid": "169.254.169.254",
        "cdn.example.invalid": "93.184.216.34", "other.corp.invalid": "10.0.0.9",
    }))
    session = SimpleNamespace(patterns=["https://intranet.corp.invalid/*", "https://*.corp.invalid/app/*"])
    decide = browser_egress_decider(session)
    assert host_allowlisted("intranet.corp.invalid", session.patterns)
    assert str(decide("intranet.corp.invalid", 443)) == "10.0.0.8"     # allowlisted internal host
    assert str(decide("cdn.example.invalid", 443)) == "93.184.216.34"  # public asset
    with pytest.raises(UnsafeURLError):
        decide("evil.corp.invalid", 443)                               # allowlisted but metadata
    with pytest.raises(UnsafeURLError):
        decide("10.0.0.7", 80)                                         # private, not allowlisted
    monkeypatch.setattr(egress, "block_private_outbound", lambda: True)
    with pytest.raises(UnsafeURLError):
        decide("intranet.corp.invalid", 443)                           # flag on


def test_egress_proxy_refuses_redirect_hops_chromium_follows_on_its_own():
    """The shared proxy, driven by real Chromium: a 302 from an allowed origin
    to anywhere else is refused (route handlers never see redirect hops)."""
    from playwright.async_api import async_playwright

    from app.core.render_guard import RenderEgressProxy, RendererPolicy, guard_page_requests

    async def run(origin, internal):
        pol = RendererPolicy([origin])
        async with RenderEgressProxy(pol.decide) as proxy, async_playwright() as p:
            b = await p.chromium.launch(headless=True, **proxy.launch_kwargs())
            page = await b.new_page()
            await guard_page_requests(page, pol.allows_url)
            await page.goto(f"{origin}/start")
            await page.evaluate(
                "(u) => Promise.all([fetch(u).catch(() => 0), new Promise(r => {"
                " const i = new Image(); i.onload = i.onerror = r; i.src = u + '?img'; })])",
                f"{origin}/redirect",
            )
            await b.close()
            return list(proxy.refused)

    with _server("127.0.0.2") as (internal, internal_seen):
        routes = {"/redirect": (302, {"Location": f"{internal}/x"}, b"")}
        with _server("127.0.0.1", routes) as (origin, origin_seen):
            refused = asyncio.run(run(origin, internal))
    assert "/start" in origin_seen and "/redirect" in origin_seen
    assert internal_seen == []
    assert refused

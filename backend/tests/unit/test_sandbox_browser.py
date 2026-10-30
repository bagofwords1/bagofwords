"""The `browser` helper generated code drives (Sandbox setting "Browser in code").

Real Chromium against a local site; only the site is a fixture. Contract:
- the code can read pages that need JavaScript and click through them;
- with "Allow form submissions from code" off (the default), a click that
  would POST is blocked before it leaves the browser; on, it goes through;
- the per-run page budget and the connection's URL allowlist hold.
"""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.ai.code_execution.sandbox_helpers import SandboxBrowser, SandboxBrowserError

HOME = b"""<!doctype html><html><body>
<ul id="items"></ul>
<a id="next" href="/page2">Next</a>
<form method="post" action="/submit"><input name="q" value="x"><button id="send">Send</button></form>
<script>
  for (const n of ["Acme", "Globex", "Initech"]) {
    const li = document.createElement("li"); li.className = "item";
    const a = document.createElement("a"); a.href = "/co/" + n.toLowerCase(); a.textContent = n;
    li.appendChild(a);
    for (const t of ["tag-" + n.length, "all"]) {
      const s = document.createElement("span"); s.className = "tag"; s.textContent = t; li.appendChild(s);
    }
    document.getElementById("items").appendChild(li);
  }
</script></body></html>"""
PAGE2 = b'<!doctype html><html><body><h1>Page two</h1><a id="back" href="/">Back</a></body></html>'


class _Site:
    def __init__(self):
        self.posts = 0
        site = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = PAGE2 if self.path.startswith("/page2") else HOME
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                site.posts += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body>sent</body></html>")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def site():
    s = _Site()
    yield s
    s.close()


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        from app.ai.tools.implementations._browser_common import chromium_executable

        with sync_playwright() as p:
            kw = {"headless": True}
            if chromium_executable():
                kw["executable_path"] = chromium_executable()
            p.chromium.launch(**kw).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="Chromium is not available")


def _drive(site, steps, **kw):
    """Run `steps(browser)` the way the sandbox does: on a worker thread,
    with Playwright on the event loop."""
    async def main():
        loop = asyncio.get_running_loop()
        b = SandboxBrowser(loop, patterns=[f"{site.base}/**"], **{"allow_mutating_requests": False, "max_pages": 10, **kw})
        try:
            return b, await loop.run_in_executor(None, steps, b)
        finally:
            await loop.run_in_executor(None, b.close)

    return asyncio.run(main())


def test_code_reads_javascript_rendered_items_and_follows_a_link(site):
    def steps(b):
        b.call("goto", [site.base + "/"], {})
        links = b.call("query_all", ["li.item a"], {"attrs": ["href"]})
        records = b.call("query_all", ["li.item"], {"fields": {"name": "a", "url": "a@href", "tags": [".tag"]}})
        # A selector the element itself matches reads the element: the
        # pagination idiom query_all("li.next a", fields={"href": "a@href"}).
        nxt = b.call("query_all", ["#next"], {"fields": {"href": "a@href", "self": "@href"}})
        b.call("click", ["#next"], {})
        return links, records, nxt, b.call("text", [], {"selector": "h1"}), b.call("url", [], {})

    _b, (links, records, nxt, heading, url) = _drive(site, steps)
    assert nxt[0]["href"] == nxt[0]["self"] == f"{site.base}/page2"
    assert [i["text"] for i in links] == ["Acme", "Globex", "Initech"]
    assert links[0]["href"] == f"{site.base}/co/acme"  # absolute
    # One record per item, read inside each element.
    assert [(r["name"], r["url"], r["tags"]) for r in records] == [
        ("Acme", f"{site.base}/co/acme", ["tag-4", "all"]),
        ("Globex", f"{site.base}/co/globex", ["tag-6", "all"]),
        ("Initech", f"{site.base}/co/initech", ["tag-7", "all"]),
    ]
    assert heading == "Page two" and url.endswith("/page2")


@pytest.mark.parametrize("allow, expect_posts", [(False, 0), (True, 1)], ids=["blocked-by-default", "allowed"])
def test_form_submission_follows_the_setting(site, allow, expect_posts):
    def steps(b):
        b.call("goto", [site.base + "/"], {})
        try:
            b.call("click", ["#send"], {})
        except Exception:
            pass
        return None

    b, _ = _drive(site, steps, allow_mutating_requests=allow)
    assert site.posts == expect_posts
    assert (b.blocked_requests > 0) is (not allow)


@pytest.mark.parametrize("second", ["goto", "click"])
def test_page_budget_counts_every_page_load(site, second):
    """Clicking through pagination loads pages too, not only goto."""
    def steps(b):
        b.call("goto", [site.base + "/"], {})
        if second == "goto":
            b.call("goto", [site.base + "/page2"], {})
        else:
            b.call("click", ["#next"], {})
        with pytest.raises(SandboxBrowserError, match="at most 2 pages"):
            b.call("click", ["#back"], {})
        return b.pages_loaded

    assert _drive(site, steps, max_pages=2)[1] == 2


def test_pages_outside_the_allowlist_do_not_open(site):
    other = _Site()
    try:
        def steps(b):
            with pytest.raises(SandboxBrowserError, match="allowlist"):
                b.call("goto", [other.base + "/"], {})
            return True

        assert _drive(site, steps)[1] is True
    finally:
        other.close()


def test_only_the_documented_operations_exist(site):
    def steps(b):
        with pytest.raises(SandboxBrowserError, match="not available"):
            b.call("evaluate", ["() => document.cookie"], {})
        return True

    assert _drive(site, steps)[1] is True

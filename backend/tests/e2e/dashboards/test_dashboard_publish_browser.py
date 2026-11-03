"""Publish a dashboard whose step reads a website with the `browser` helper.

The agent builds such a step with a headless browser; Publish (and a
refresh, and a reader's run) re-executes it on its own. Contract:
- with "Browser in code" on and a browser connection on the dashboard's
  agent, the step runs again in a real browser and the release goes live
  with what the site shows now;
- without one, Publish fails with a reason the owner can act on, never a
  Python `NoneType` error, and readers keep the live release.

Real Chromium against a local site (the only fixture); everything else is
the real route, background run and services.
"""
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.e2e.dashboards.dashboard_setup import as_, own, publish
from tests.e2e.dashboards.test_dashboard_publish import (  # noqa: F401  (fixtures)
    _copies, _finish, _months, _post, _status, drawn, spawned,
)

pytestmark = pytest.mark.e2e


def _site_page(months) -> bytes:
    # Rendered by JavaScript: only a browser sees the rows.
    items = ",".join(f'"{m}"' for m in months)
    return (
        "<!doctype html><html><head><meta charset='utf-8'></head><body><ul id='m'></ul><script>"
        f"for (const n of [{items}]) {{ const li = document.createElement('li'); li.className = 'month';"
        " li.textContent = n; document.getElementById('m').appendChild(li); }"
        "</script></body></html>"
    ).encode("utf-8")


@pytest.fixture
def site():
    state = {"months": ["מרץ", "אפריל", "מאי"]}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_site_page(state["months"]))

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["base"] = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()


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


def _browser_code(base) -> str:
    return (
        "def generate_df(ds_clients, excel_files, browser):\n"
        f"    browser.goto({base + '/'!r})\n"
        "    items = browser.query_all('li.month')\n"
        "    return pd.DataFrame([{'חודש': i['text'], 'הכנסות': n} for n, i in enumerate(items)])\n"
    )


def _attach_browser_connection(test_client, corpus, base) -> None:
    """A browser connection on the dashboard's agent, through the API."""
    admin = as_(corpus, "admin")
    conn = test_client.post("/api/connections", headers=admin, json={
        "name": f"site {uuid.uuid4().hex[:6]}", "type": "browser", "auth_policy": "system_only",
        "config": {"url_patterns": [f"{base}/**"], "allow_downloads": False}, "credentials": {},
    })
    assert conn.status_code == 200, conn.text
    ds = corpus.cast.data_sources["chinook"]
    attached = test_client.post(f"/api/data_sources/{ds}/connections/{conn.json()['id']}?sync_tables=false",
                                headers=admin)
    assert attached.status_code == 200, attached.text


def _browser_in_code(test_client, corpus, on: bool) -> None:
    resp = test_client.put("/api/organization/settings", headers=as_(corpus, "admin"),
                           json={"config": {"enable_sandbox_browser": {"value": on}}})
    assert resp.status_code == 200, resp.text


@pytest.mark.skipif(not _chromium_available(), reason="Chromium is not available")
def test_publish_reruns_a_browser_step_in_a_real_browser(corpus, corpus_rows, test_client, spawned, site):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    publish(aid)
    _attach_browser_connection(test_client, corpus, site["base"])
    _browser_in_code(test_client, corpus, True)
    corpus_rows.update("steps", corpus.ids["conv_three_dash/q_sales/step"], code=_browser_code(site["base"]))

    site["months"] = ["יוני", "יולי"]  # what the site shows at publish time, not when the step was built
    new = _post(test_client, corpus, aid).json()["latest"]["id"]
    _finish(spawned)
    status = _status(test_client, corpus, aid)
    assert status["latest"]["status"] == "ready", status["latest"]["error"]
    assert status["published"]["id"] == new
    (copy,) = _copies(corpus_rows, new)
    assert _months(copy["data"]) == {"יוני", "יולי"}


@pytest.mark.parametrize("setting_on", [False, True], ids=["setting-off", "no-browser-connection"])
def test_publish_without_a_browser_fails_with_a_reason(corpus, corpus_rows, test_client, spawned, site, setting_on):
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    live = publish(aid)
    _browser_in_code(test_client, corpus, setting_on)
    corpus_rows.update("steps", corpus.ids["conv_three_dash/q_sales/step"], code=_browser_code(site["base"]))

    attempt = _post(test_client, corpus, aid).json()["latest"]["id"]
    _finish(spawned)
    status = _status(test_client, corpus, aid)
    assert status["latest"]["id"] == attempt and status["latest"]["status"] == "failed"
    error = status["latest"]["error"]
    assert "browser" in error and "NoneType" not in error
    assert ("setting is off" in error) is (not setting_on)
    assert status["published"]["id"] == live  # readers keep the live release

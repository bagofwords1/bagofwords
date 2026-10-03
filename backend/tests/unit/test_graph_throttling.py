"""Microsoft Graph throttling discipline in GraphDriveClient.

Microsoft's SharePoint / OneDrive limits are fixed per app per tenant and
cannot be raised; a client that ignores 429 / Retry-After is escalated to a
full block. These tests pin the behaviours that keep us out of that state:

- 429 / 503 are retried after the server's Retry-After, and the wait is shared
  by every client on the same app registration.
- Past the retry budget the failure is a GraphThrottledError with the
  retry-after attached (the tool layer turns it into an actionable message).
- Every request carries the decorated User-Agent Microsoft asks for.
- Child listings ask for only the fields we use ($select).
- Recursive enumeration of the scoped root goes through /delta (one request
  per page, not one per folder), with a fallback to the folder walk.
- Listings are cached briefly so repeated live listings don't re-walk.

Run:
    cd backend && python -m pytest tests/unit/test_graph_throttling.py -v
"""
from __future__ import annotations

import json
from typing import Callable, List

import httpx
import pytest

from app.data_sources.clients import _graph_throttle as gt
from app.data_sources.clients import graph_drive_client as gdc
from app.data_sources.clients.graph_drive_client import GraphDriveClient
from app.ai.tools.implementations._file_tool_common import friendly_tool_error


SITE = "contoso.sharepoint.com,site-guid,web-guid"
DRIVE = "drv-docs"


class FakeClock:
    """A monotonic clock that only `sleep` advances, so shared pauses expire
    exactly the way they would in real time without the test waiting."""

    def __init__(self):
        self.now = 1000.0
        self.slept: List[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    """Fresh shared state per test, and no real sleeping."""
    gt.reset_throttle_states()
    gt.listing_cache.clear()
    clock = FakeClock()
    monkeypatch.setattr(gdc.time, "sleep", clock.sleep)
    monkeypatch.setattr(gt.time, "monotonic", clock.monotonic)
    yield clock
    gt.reset_throttle_states()
    gt.listing_cache.clear()


@pytest.fixture
def _isolate(_clock):
    return _clock.slept


def _json(status: int, body, headers=None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers or {})


def _client(handler: Callable[[httpx.Request], httpx.Response], **kw) -> GraphDriveClient:
    kw.setdefault("tenant_id", "tenant-a")
    kw.setdefault("client_id", "app-a")
    kw.setdefault("client_secret", "s")
    c = GraphDriveClient(
        site_url="https://contoso.sharepoint.com/sites/hr", mode="sharepoint", **kw,
    )
    c.access_token = "tok"  # skip the token endpoint
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    return c


class Recorder:
    """A scripted Graph: `script` maps a URL substring to a list of responses
    consumed in order (the last one repeats)."""

    def __init__(self):
        self.requests: List[httpx.Request] = []
        self.script = {}

    def on(self, needle: str, *responses: httpx.Response):
        self.script[needle] = list(responses)
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for needle, responses in self.script.items():
            if needle in url:
                return responses.pop(0) if len(responses) > 1 else responses[0]
        return _json(404, {"error": {"message": f"unscripted {url}"}})


# ------------------------------------------------------------ retry / pause

def test_429_is_retried_after_retry_after(_isolate):
    rec = Recorder().on(
        "/sites/", _json(429, {"error": "throttled"}, {"Retry-After": "7"}), _json(200, {"id": SITE})
    )
    c = _client(rec)
    assert c._resolve_site_id() == SITE
    assert len(rec.requests) == 2
    assert _isolate == [7.0]


def test_503_uses_backoff_when_no_retry_after(_isolate):
    rec = Recorder().on("/sites/", _json(503, {"error": "busy"}), _json(200, {"id": SITE}))
    c = _client(rec)
    assert c._resolve_site_id() == SITE
    assert len(_isolate) == 1 and 1.0 <= _isolate[0] <= 3.0


def test_throttled_past_budget_raises_graph_throttled_error(_isolate, monkeypatch):
    monkeypatch.setattr(gdc, "GRAPH_MAX_ATTEMPTS", 3)
    rec = Recorder().on("/sites/", _json(429, {"error": "throttled"}, {"Retry-After": "5"}))
    c = _client(rec)
    with pytest.raises(gt.GraphThrottledError) as ei:
        c._resolve_site_id()
    assert ei.value.status == 429
    assert ei.value.retry_after == 5.0
    assert "retry in about 5s" in str(ei.value)
    # 3 attempts → 2 sleeps; the third 429 gives up.
    assert len(rec.requests) == 3
    assert _isolate == [5.0, 5.0]


def test_long_retry_after_fails_fast_instead_of_stalling(_isolate):
    rec = Recorder().on("/sites/", _json(429, {}, {"Retry-After": "300"}))
    c = _client(rec)
    with pytest.raises(gt.GraphThrottledError) as ei:
        c._resolve_site_id()
    assert ei.value.retry_after == 300.0
    assert len(rec.requests) == 1 and _isolate == []


def test_pause_is_shared_across_clients_on_same_app(_isolate):
    """One client's 429 must stop a *different* client instance (another user,
    another connection, another thread) from sending until Retry-After has
    elapsed — that is what keeps the app from being escalated to a block."""
    rec_a = Recorder().on("/sites/", _json(429, {}, {"Retry-After": "40"}))
    a = _client(rec_a)
    with pytest.raises(gt.GraphThrottledError):
        a._resolve_site_id()

    rec_b = Recorder().on("/sites/", _json(200, {"id": SITE}))
    b = _client(rec_b)
    with pytest.raises(gt.GraphThrottledError) as ei:
        b._resolve_site_id()
    assert rec_b.requests == []  # never sent while paused
    assert 0 < ei.value.retry_after <= 40
    assert "paused after earlier throttling" in str(ei.value)

    # A different app registration is unaffected.
    rec_c = Recorder().on("/sites/", _json(200, {"id": SITE}))
    c = _client(rec_c, tenant_id="tenant-b")
    assert c._resolve_site_id() == SITE


def test_short_shared_pause_is_waited_out(_isolate):
    rec_a = Recorder().on("/sites/", _json(429, {}, {"Retry-After": "3"}), _json(200, {"id": SITE}))
    a = _client(rec_a)
    assert a._resolve_site_id() == SITE
    assert _isolate == [3.0]
    # A second client arriving mid-pause waits out the remainder, then sends.
    gt.throttle_state_for(a._throttle_key()).pause_for(2.0)
    rec_b = Recorder().on("/sites/", _json(200, {"id": SITE}))
    b = _client(rec_b)
    assert b._resolve_site_id() == SITE
    assert len(rec_b.requests) == 1
    assert len(_isolate) == 2 and 0 < _isolate[1] <= 2.0


def test_ratelimit_headers_pause_proactively(_isolate):
    rec = Recorder().on(
        "/sites/",
        _json(200, {"id": SITE}, {"RateLimit-Limit": "1200", "RateLimit-Remaining": "3", "RateLimit-Reset": "12"}),
    )
    c = _client(rec)
    assert c._resolve_site_id() == SITE
    state = gt.throttle_state_for(c._throttle_key())
    assert 0 < state.remaining_pause() <= 12


def test_non_retryable_4xx_is_not_retried(_isolate):
    rec = Recorder().on("/sites/", _json(404, {"error": "nope"}))
    c = _client(rec)
    with pytest.raises(ValueError) as ei:
        c._resolve_site_id()
    assert not isinstance(ei.value, gt.GraphThrottledError)
    assert len(rec.requests) == 1 and _isolate == []


def test_401_remint_still_works_and_is_not_a_retry(_isolate, monkeypatch):
    rec = Recorder().on("/sites/", _json(401, {"error": "expired"}), _json(200, {"id": SITE}))
    c = _client(rec)
    minted = []
    monkeypatch.setattr(c, "_token", lambda: minted.append(1) or "tok2")
    assert c._resolve_site_id() == SITE
    assert len(rec.requests) == 2
    assert rec.requests[1].headers["Authorization"] == "Bearer tok2"


def test_transport_errors_are_retried(_isolate):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return _json(200, {"id": SITE})

    c = _client(handler)
    assert c._resolve_site_id() == SITE
    assert len(calls) == 2


# ------------------------------------------------------- request decoration

def test_requests_carry_decorated_user_agent_and_select():
    rec = Recorder().on("/children", _json(200, {"value": []}))
    c = _client(rec)
    c._list_children(DRIVE, "root-id")
    req = rec.requests[0]
    assert req.headers["User-Agent"].startswith("ISV|BagOfWords|bagofwords/")
    assert "$select=id,name,size,file,folder" in str(req.url)
    assert "$top=200" in str(req.url)


def test_tool_layer_message_for_throttling():
    err = gt.GraphThrottledError("https://graph/x", 429, 42.0)
    msg = friendly_tool_error("list_files", "HR SharePoint", err)
    assert "rate-limiting 'HR SharePoint'" in msg
    assert "about 42s" in msg
    assert "Do NOT retry" in msg


# ------------------------------------------------------------- delta walk

def _delta_page(items, next_link=None):
    body = {"value": items}
    if next_link:
        body["@odata.nextLink"] = next_link
    return _json(200, body)


def _item(iid, name, parent_path, folder=False):
    e = {"id": iid, "name": name, "parentReference": {"path": parent_path}, "size": 10,
         "lastModifiedDateTime": "2026-01-01T00:00:00Z", "webUrl": f"https://x/{name}"}
    if folder:
        e["folder"] = {"childCount": 1}
    else:
        e["file"] = {"mimeType": "application/octet-stream"}
    return e


def _recursive_client(rec, **kw):
    c = _client(rec, recursive=True, **kw)
    c._site_id = SITE
    c._drives = [(DRIVE, "Documents")]
    c._root_item_ids[DRIVE] = "root-id"
    c._root_item_id = "root-id"
    return c


def test_recursive_listing_uses_delta_not_per_folder_children():
    root = f"/drives/{DRIVE}/root:"
    rec = Recorder().on(
        "/delta",
        _delta_page([
            _item("root-id", "root", None, folder=True),
            _item("f1", "Reports", root, folder=True),
            _item("a", "b.xlsx", root),
            _item("d", "gone.csv", root) | {"deleted": {"state": "deleted"}},
        ], next_link=f"{gdc.GRAPH_BASE}/drives/{DRIVE}/items/root-id/delta?token=2"),
        _delta_page([
            _item("f2", "2025", f"{root}/Reports", folder=True),
            _item("c", "q1.csv", f"{root}/Reports/2025"),
        ]),
    )
    c = _recursive_client(rec)
    rows = c.list_files()
    assert [r["path"] for r in rows] == ["b.xlsx", "Reports/2025/q1.csv"]
    assert all("/children" not in str(r.url) for r in rec.requests)
    assert sum("/delta" in str(r.url) for r in rec.requests) == 2
    assert "$select=" in str(rec.requests[0].url)


def test_delta_paths_are_relative_to_folder_path_and_glob_scoped():
    root = f"/drives/{DRIVE}/root:"
    rec = Recorder().on("/delta", _delta_page([
        _item("a", "b.xlsx", f"{root}/Finance"),
        _item("c", "notes.txt", f"{root}/Finance/Sub"),
        _item("e", "k.xlsx", f"{root}/Finance/Sub"),
    ]))
    c = _recursive_client(rec, folder_path="Finance", include_globs="**/*.xlsx")
    rows = c.list_files()
    assert [r["path"] for r in rows] == ["b.xlsx", "Sub/k.xlsx"]


def test_delta_prefixes_library_name_for_all_libraries_connections():
    root = f"/drives/{DRIVE}/root:"
    rec = Recorder().on("/delta", _delta_page([_item("a", "b.xlsx", f"{root}/X")]))
    c = _recursive_client(rec, drive_name="*")
    rows = c.list_files()
    assert rows[0]["path"] == "Documents/X/b.xlsx"
    assert rows[0]["id"] == f"{DRIVE}|a"


def test_delta_rejected_falls_back_to_folder_walk():
    root = f"/drives/{DRIVE}/root:"
    rec = (Recorder()
        .on("/delta", _json(400, {"error": "not supported"}))
        .on("/items/root-id/children", _json(200, {"value": [_item("a", "b.xlsx", root)]})))
    c = _recursive_client(rec)
    rows = c.list_files()
    assert [r["path"] for r in rows] == ["b.xlsx"]
    assert any("/children" in str(r.url) for r in rec.requests)


def test_delta_throttling_is_not_swallowed_into_a_walk(_isolate):
    rec = Recorder().on("/delta", _json(429, {}, {"Retry-After": "500"}))
    c = _recursive_client(rec)
    with pytest.raises(gt.GraphThrottledError):
        c.list_files()
    assert not any("/children" in str(r.url) for r in rec.requests)


def test_subfolder_browse_keeps_folder_walk():
    """`list_files(folder_id=...)` on a non-root folder stays on `children`:
    delta paths are drive-relative, the walk's are folder-relative."""
    rec = Recorder().on("/items/sub-id/children", _json(200, {"value": [_item("a", "b.xlsx", "x")]}))
    c = _recursive_client(rec)
    rows = c.list_files(folder_id="sub-id")
    assert [r["path"] for r in rows] == ["b.xlsx"]
    assert not any("/delta" in str(r.url) for r in rec.requests)


# ------------------------------------------------------------ listing cache

def test_listing_is_cached_briefly_per_identity():
    rec = Recorder().on("/delta", _delta_page([_item("a", "b.xlsx", f"/drives/{DRIVE}/root:")]))
    c = _recursive_client(rec)
    first = c.list_files()
    second = c.list_files()
    assert first == second
    assert sum("/delta" in str(r.url) for r in rec.requests) == 1
    # Mutating a returned row must not poison the cache.
    second[0]["name"] = "hacked"
    assert c.list_files()[0]["name"] == "b.xlsx"

    # A different signed-in user (different delegated token) gets a fresh walk.
    rec2 = Recorder().on("/delta", _delta_page([_item("z", "other.xlsx", f"/drives/{DRIVE}/root:")]))
    other = GraphDriveClient(
        site_url="https://contoso.sharepoint.com/sites/hr", mode="sharepoint",
        tenant_id="tenant-a", client_id="app-a", client_secret="s",
        access_token="user-token-2", recursive=True,
    )
    other._http = httpx.Client(transport=httpx.MockTransport(rec2))
    other._site_id = SITE
    other._drives = [(DRIVE, "Documents")]
    other._root_item_ids[DRIVE] = "root-id"
    other._root_item_id = "root-id"
    assert [r["name"] for r in other.list_files()] == ["other.xlsx"]


def test_walk_concurrency_default_and_config_field():
    from app.schemas.data_sources.configs import SharePointConfig, OneDriveConfig
    assert gdc.WALK_CONCURRENCY_DEFAULT == 4
    assert SharePointConfig(site_url="https://x.sharepoint.com/sites/a").walk_concurrency == 4
    assert OneDriveConfig().walk_concurrency == 4
    with pytest.raises(Exception):
        SharePointConfig(site_url="https://x.sharepoint.com/sites/a", walk_concurrency=0)
    c = GraphDriveClient(site_url="https://x.sharepoint.com/sites/a", access_token="t", walk_concurrency=2)
    assert c.walk_concurrency == 2

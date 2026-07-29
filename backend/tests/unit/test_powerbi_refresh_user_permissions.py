"""Power BI caches user permissions; we flush it on delegated crawls.

Power BI serves workspace/dataset permissions from a replicated cache, so a
grant or revocation made in the portal lags by an unbounded window (measured
live: a Member→Viewer demotion still returned unfiltered rows until
RefreshUserPermissions was called). We flush it via POST /myorg/
RefreshUserPermissions at the two moments a user's access may just have changed
and we are about to build their catalog: OBO sign-in and manual reload. Both go
through get_schemas on a delegated client, so that is where the flush is wired.

Rules pinned here:
  - a delegated crawl flushes exactly once,
  - the service principal never flushes (it has no user permission cache),
  - the flush never fires on the query path (dataset IDs resolve from attached
    metadata; get_schemas is not reached),
  - a flush failure never breaks discovery.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from app.data_sources.clients.powerbi_client import PowerBIClient


def _resp(status: int, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.headers = {}
    r.text = json.dumps(payload) if payload else ""
    return r


class _Tenant:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self._routes: list[tuple[str, str, object]] = []

    def route(self, method, sub, response):
        self._routes.append((method.upper(), sub, response))

    def _dispatch(self, method, url):
        self.calls.append((method.upper(), url))
        matches = [(len(s), r) for m, s, r in self._routes if m == method.upper() and s in url]
        if matches:
            return max(matches, key=lambda x: x[0])[1]
        return _resp(404, {"error": {"code": "PowerBIEntityNotFound"}})

    def request(self, method, url, json=None, headers=None, timeout=None):
        return self._dispatch(method, url)

    def post(self, url, json=None, headers=None, timeout=None):
        return self._dispatch("POST", url)

    def get(self, url, headers=None, timeout=None):
        return self._dispatch("GET", url)

    def count(self, method, sub):
        return sum(1 for m, u in self.calls if m == method.upper() and sub in u)


def _delegated(tenant):
    c = PowerBIClient(tenant_id="t", client_id="c", client_secret="s", access_token="user-tok")
    c._http = tenant
    return c


def _service_principal(tenant):
    c = PowerBIClient(tenant_id="t", client_id="c", client_secret="s")
    c._http = tenant
    c._access_token = "sp-tok"  # pretend client-credentials already ran
    return c


REFRESH = "/v1.0/myorg/RefreshUserPermissions"


def _empty_listing(tenant):
    tenant.route("GET", "/groups", _resp(200, {"value": []}))


def test_delegated_crawl_flushes_permissions_once():
    t = _Tenant()
    _empty_listing(t)
    t.route("POST", REFRESH, _resp(200))

    c = _delegated(t)
    c.get_schemas()
    c.get_schemas()          # cached — no second crawl
    c.get_schemas(force_refresh=True)   # re-crawl, but flush already spent

    assert t.count("POST", REFRESH) == 1


def test_flush_does_not_retry_on_429(monkeypatch):
    """RefreshUserPermissions is aggressively rate-limited (429 + ~30s
    Retry-After). The flush must NOT ride the shared _request backoff loop — that
    would block the overlay sync, and thus interactive sign-in, for up to a
    minute on a best-effort call. One attempt, then move on."""
    import time as _time

    # Record backoff sleeps rather than raising: the flush's own try/except would
    # swallow a raised assertion, hiding the retry. _request does a local
    # `import time`, which returns this same module object.
    sleeps: list = []
    monkeypatch.setattr(_time, "sleep", lambda s, *a, **k: sleeps.append(s))

    t = _Tenant()
    _empty_listing(t)
    t.route("POST", REFRESH, _resp(429))   # rate limited, every time

    _delegated(t).get_schemas()

    # One attempt, zero backoff sleeps. With the shared retry loop this would be
    # 3 POSTs and 2 sleeps — up to ~60s of blocking on a best-effort call.
    assert t.count("POST", REFRESH) == 1
    assert sleeps == []


def test_service_principal_never_flushes():
    t = _Tenant()
    _empty_listing(t)
    t.route("POST", REFRESH, _resp(200))

    _service_principal(t).get_schemas()

    assert t.count("POST", REFRESH) == 0


def test_flush_failure_does_not_break_discovery():
    t = _Tenant()
    tables = {
        "m/T": {"columns": [{"name": "id", "dtype": "int64"}], "pks": [], "fks": [],
                "metadata_json": {"powerbi": {"datasetId": "ds1", "workspaceId": "ws1",
                                              "datasetName": "m", "tableName": "T"}}}
    }
    # /groups returns the workspace so the model is discovered the normal way.
    t.route("GET", "/groups", _resp(200, {"value": [{"id": "ws1", "name": "m"}]}))
    t.route("GET", "/groups/ws1/datasets", _resp(200, {"value": [{"id": "ds1", "name": "m"}]}))
    t.route("GET", "/groups/ws1/reports", _resp(200, {"value": []}))
    t.route("POST", REFRESH, _resp(500))   # flush fails

    out = _delegated(t).get_schemas(prior_tables=tables)

    assert [x.name for x in out] == ["m/T"]


def test_query_path_does_not_flush():
    """execute_query resolves the dataset from attached metadata and never calls
    get_schemas, so a hot query must not trigger a permission flush."""
    t = _Tenant()
    meta = {"powerbi": {"datasetId": "ds1", "workspaceId": "ws1",
                        "datasetName": "m", "tableName": "T"}}
    t.route("POST", "/groups/ws1/datasets/ds1/executeQueries",
            _resp(200, {"results": [{"tables": [{"rows": [{"T[id]": 1}]}]}]}))
    t.route("POST", REFRESH, _resp(200))

    c = _delegated(t)
    c.attach_table_metadata([{"name": "m/T", "metadata_json": meta}])
    c.execute_query("EVALUATE T", "m/T")

    assert t.count("POST", REFRESH) == 0

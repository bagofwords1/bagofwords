"""Connection create / update / test never connect to cloud metadata, and a
connection to an internal (private-network) server keeps working.

Through the real routes: POST /api/connections, PUT /api/connections/{id}
(revalidation on config change) and POST /api/connections/{id}/test with
config overrides. The data source is a Prometheus server (a plain HTTP API,
faked on 127.0.0.1); the network is played at the socket boundary by
``NetTrap`` — every connect to 169.254.169.254 lands on a metadata stand-in
whose hit list must stay empty.
"""
import asyncio
import threading
import uuid

import pytest

from app.core import egress
from app.dependencies import async_session_maker
from app.services.connection_indexing_service import ConnectionIndexingService
from tests.utils.egress_fakes import NetTrap, local_server


class _Prometheus(dict):
    """Any path -> a successful, empty Prometheus API answer."""

    def get(self, key, default=None):
        path = key[1] if isinstance(key, tuple) else key
        path = str(path).split("?")[0]
        data = {
            "/api/v1/query": {"resultType": "scalar", "result": [0, "1"]},
            "/api/v1/label/__name__/values": ["up"],
            "/api/v1/metadata": {"up": [{"type": "gauge", "help": "target up", "unit": ""}]},
            "/api/v1/series": [{"__name__": "up", "job": "node"}],
            "/api/v1/labels": ["__name__", "job"],
        }.get(path, [])
        return (200, {}, {"status": "success", "data": data})


@pytest.fixture
def net(monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(egress, "block_private_outbound", lambda: False)
    with local_server(_Prometheus()) as (port, seen):
        trap = NetTrap(monkeypatch, {"imds.corp.invalid": "169.254.169.254"})
        try:
            yield trap, port, seen
        finally:
            trap.close()


@pytest.fixture
def admin(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _create(test_client, headers, base_url):
    return test_client.post("/api/connections", headers=headers, json={
        "name": f"prom-{uuid.uuid4().hex[:6]}", "type": "prometheus",
        "config": {"base_url": base_url}, "credentials": {}, "auth_policy": "system_only",
    })


def _wait_for_indexing(conn_id):
    """Block until the connection's background indexing has finished."""
    async def wait():
        async with async_session_maker() as db:
            await ConnectionIndexingService().wait_for_active(db, str(conn_id), timeout_s=120.0)
    asyncio.run(wait())


@pytest.mark.e2e
def test_connection_to_internal_server_works_and_update_to_metadata_is_refused(test_client, admin, net):
    trap, port, seen = net
    trap.table["prom.corp.invalid"] = ["127.0.0.1"]
    created = _create(test_client, admin, f"http://prom.corp.invalid:{port}")
    assert created.status_code == 200, created.text
    assert seen, "the internal server was validated against"
    conn_id = created.json()["id"]

    for target in ("http://imds.corp.invalid", "http://169.254.169.254", "http://0xa9fea9fe:8080"):
        r = test_client.put(f"/api/connections/{conn_id}", headers=admin, json={"config": {"base_url": target}})
        assert r.status_code == 400, (target, r.text)
    assert trap.hits == []

    # The saved (internal) config is untouched and still tests fine.
    r = test_client.post(f"/api/connections/{conn_id}/test", headers=admin, json={})
    assert r.status_code == 200 and r.json()["success"] is True


@pytest.mark.e2e
def test_connection_test_with_overrides_never_reaches_metadata(test_client, admin, net):
    trap, port, _ = net
    trap.table["prom.corp.invalid"] = ["127.0.0.1"]
    conn_id = _create(test_client, admin, f"http://prom.corp.invalid:{port}").json()["id"]

    for target in ("http://imds.corp.invalid", "http://169.254.169.254:9090", "http://2852039166"):
        r = test_client.post(f"/api/connections/{conn_id}/test", headers=admin, json={"config": {"base_url": target}})
        assert r.status_code == 200 and r.json()["success"] is False, (target, r.text)
    assert trap.hits == []


@pytest.mark.e2e
def test_connection_rebound_to_metadata_after_save_is_refused_at_test_time(test_client, admin, net):
    """Saved while the name pointed at the internal server; the name now
    answers with the metadata address. The use-time check refuses it."""
    trap, port, seen = net
    trap.table["rebind.corp.invalid"] = ["127.0.0.1"]
    conn_id = _create(test_client, admin, f"http://rebind.corp.invalid:{port}").json()["id"]
    trap.table["rebind.corp.invalid"] = ["169.254.169.254"]

    r = test_client.post(f"/api/connections/{conn_id}/test", headers=admin, json={})
    assert r.status_code == 200 and r.json()["success"] is False
    assert trap.hits == []


class _HeldIndexing(_Prometheus):
    """Holds the second metric-name listing (the first is the create-time
    validation, the second the background indexing) until the test releases it."""

    def __init__(self):
        super().__init__()
        self.listings = 0
        self.indexing_started, self.release = threading.Event(), threading.Event()

    def get(self, key, default=None):
        if str(key[1] if isinstance(key, tuple) else key) == "/api/v1/label/__name__/values":
            self.listings += 1
            if self.listings == 2:
                self.indexing_started.set()
                self.release.wait(20)
        return super().get(key, default)


@pytest.mark.e2e
def test_changed_name_follows_the_policy_during_background_indexing(test_client, admin, net):
    """Indexing is built and checked before the name changes; its later
    requests must apply the policy to the new answer."""
    trap, _, _ = net
    routes = _HeldIndexing()
    with local_server(routes) as (port, _):
        trap.table["rebind.corp.invalid"] = ["127.0.0.1"]
        conn_id = _create(test_client, admin, f"http://rebind.corp.invalid:{port}").json()["id"]
        assert routes.indexing_started.wait(10)
        trap.table["rebind.corp.invalid"] = ["169.254.169.254"]
        routes.release.set()
        _wait_for_indexing(conn_id)
    assert trap.hits == []


@pytest.mark.e2e
def test_name_answering_differently_at_connect_follows_the_policy(test_client, admin, net):
    """A name whose answer changes: the use-time check sees the internal address,
    the connection must not be made to a second answer."""
    trap, port, _ = net
    trap.table["rebind.corp.invalid"] = ["127.0.0.1"]
    conn_id = _create(test_client, admin, f"http://rebind.corp.invalid:{port}").json()["id"]
    _wait_for_indexing(conn_id)
    # One answer for the check; every later lookup answers differently.
    trap.table["rebind.corp.invalid"] = ["127.0.0.1", "169.254.169.254"]

    r = test_client.post(f"/api/connections/{conn_id}/test", headers=admin, json={})
    assert r.status_code == 200
    assert trap.hits == []


@pytest.mark.e2e
def test_redirects_follow_the_outbound_policy(test_client, admin, net):
    """The saved name stays clean; the server answers with a redirect."""
    trap, _, _ = net
    redirect = {"on": False}

    class Redirecting(_Prometheus):
        def get(self, key, default=None):
            if redirect["on"]:
                return (302, {"Location": "http://169.254.169.254/latest/user-data"}, b"")
            return super().get(key, default)

    with local_server(Redirecting()) as (port, _):
        trap.table["prom.corp.invalid"] = ["127.0.0.1"]
        conn_id = _create(test_client, admin, f"http://prom.corp.invalid:{port}").json()["id"]
        _wait_for_indexing(conn_id)
        redirect["on"] = True
        r = test_client.post(f"/api/connections/{conn_id}/test", headers=admin, json={})
    assert r.status_code == 200 and r.json()["success"] is False
    assert trap.hits == []


@pytest.mark.e2e
def test_internal_connection_refused_when_block_private_outbound(test_client, admin, net, monkeypatch):
    trap, port, seen = net
    trap.table["prom.corp.invalid"] = ["127.0.0.1"]
    conn_id = _create(test_client, admin, f"http://prom.corp.invalid:{port}").json()["id"]

    monkeypatch.setattr(egress, "block_private_outbound", lambda: True)
    seen.clear()
    r = test_client.post(f"/api/connections/{conn_id}/test", headers=admin, json={})
    assert r.json()["success"] is False
    # (the background indexing kicked off by the create may still be running
    # with its already-built client; the test probe itself is /api/v1/query)
    assert not [p for _, p, _ in seen if p.startswith("/api/v1/query")]
    assert _create(test_client, admin, f"http://prom.corp.invalid:{port}").status_code == 400

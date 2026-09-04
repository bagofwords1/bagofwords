"""Admin UI API (design C4): CRUD, credential hygiene, persistence + apply.

No broker and no real database: a fake tunnel records what the server asks it
to apply, and a real (tmp) encrypted store backs persistence. What we assert is
the server's contract — credentials never come back out, blank fields keep the
saved value, a save both persists and applies, a delete does both.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet

from ..admin.server import AdminServer
from ..audit import AuditLog
from ..config import ConnectionConfig
from ..store import ConnectionStore

pytestmark = pytest.mark.asyncio


class FakeTunnel:
    """Records applies/removes; serves connections from an in-memory list."""

    def __init__(self):
        self._conns: dict[str, ConnectionConfig] = {}
        self.applied: list[str] = []
        self.removed: list[str] = []
        self.tested: list[str] = []
        self.test_result = {"success": True, "message": "Connected"}
        self.audit = AuditLog(None)

    def status_snapshot(self):
        return {"edge_agent_id": "nyc-01", "version": "9.9",
                "nats": {"connected": True, "url": "ws://x"},
                "connections_served": len(self._conns), "in_flight": 0}

    def list_connections(self):
        return list(self._conns.values())

    def get_connection(self, name):
        return self._conns.get(name)

    async def apply_connection(self, conn):
        self._conns[conn.name] = conn
        self.applied.append(conn.name)

    async def remove_connection(self, name):
        existed = name in self._conns
        self._conns.pop(name, None)
        self.removed.append(name)
        return existed

    async def test_connection_config(self, conn):
        self.tested.append(conn.name)
        return self.test_result


@pytest.fixture
def store(tmp_path):
    return ConnectionStore(tmp_path / "store.json", Fernet.generate_key().decode())


@pytest_asyncio.fixture
async def client(tmp_path, store):
    tunnel = FakeTunnel()
    server = AdminServer(tunnel, store, "127.0.0.1", 0)
    tc = TestClient(TestServer(server.build_app()))
    await tc.start_server()
    tc.tunnel = tunnel  # type: ignore[attr-defined]
    tc.store = store    # type: ignore[attr-defined]
    yield tc
    await tc.close()


_PG = {
    "name": "lego-pg", "type": "postgresql", "label": "LEGO",
    "config": {"host": "db.internal", "port": 5432, "database": "lego"},
    "credentials": {"user": "lego", "password": "s3cret"},
}


async def test_create_persists_applies_and_hides_credentials(client):
    r = await client.post("/api/connections", json=_PG)
    assert r.status == 201
    body = await r.json()
    # Values never come back; only which keys are set.
    assert "credentials" not in body
    assert body["credential_keys"] == ["password", "user"]

    # Applied to the tunnel and written to the store (encrypted).
    assert client.tunnel.applied == ["lego-pg"]
    stored = client.store.get("lego-pg")
    assert stored.credentials == {"user": "lego", "password": "s3cret"}


async def test_duplicate_create_conflicts(client):
    await client.post("/api/connections", json=_PG)
    r = await client.post("/api/connections", json=_PG)
    assert r.status == 409


async def test_update_blank_password_keeps_stored_value(client):
    await client.post("/api/connections", json=_PG)
    # edit host only; password left blank
    edit = {**_PG, "config": {**_PG["config"], "host": "db2.internal"},
            "credentials": {"user": "lego", "password": ""}}
    r = await client.put("/api/connections/lego-pg", json=edit)
    assert r.status == 200
    stored = client.store.get("lego-pg")
    assert stored.config["host"] == "db2.internal"
    assert stored.credentials["password"] == "s3cret"  # unchanged


async def test_list_and_get(client):
    await client.post("/api/connections", json=_PG)
    lst = await (await client.get("/api/connections")).json()
    assert [c["name"] for c in lst["connections"]] == ["lego-pg"]
    one = await (await client.get("/api/connections/lego-pg")).json()
    assert one["type"] == "postgresql" and "credentials" not in one


async def test_delete_removes_from_store_and_tunnel(client):
    await client.post("/api/connections", json=_PG)
    r = await client.delete("/api/connections/lego-pg")
    assert r.status == 200
    assert client.tunnel.removed == ["lego-pg"]
    assert client.store.get("lego-pg") is None


async def test_delete_missing_is_404(client):
    r = await client.delete("/api/connections/ghost")
    assert r.status == 404


async def test_test_saved_and_unsaved(client):
    await client.post("/api/connections", json=_PG)
    r1 = await client.post("/api/connections/lego-pg/test")
    assert (await r1.json())["success"] is True
    # unsaved: test straight from a posted body
    r2 = await client.post("/api/test", json={**_PG, "name": "scratch"})
    assert (await r2.json())["success"] is True
    assert "scratch" in client.tunnel.tested


async def test_invalid_name_rejected(client):
    bad = {**_PG, "name": "has.dot"}  # dots break NATS subject tokens
    r = await client.post("/api/connections", json=bad)
    assert r.status == 400


async def test_status_and_types(client):
    s = await (await client.get("/api/status")).json()
    assert s["edge_agent_id"] == "nyc-01"
    t = await (await client.get("/api/types")).json()
    assert "postgresql" in t["types"] and "mysql" in t["types"]


async def test_catalog_lists_types_for_the_picker(client):
    r = await client.get("/api/catalog")
    assert r.status == 200
    cat = (await r.json())["catalog"]
    types = {row["type"] for row in cat}
    assert "postgresql" in types and "mysql" in types
    assert all({"type", "title", "category"} == set(row) for row in cat)


async def test_catalog_type_returns_field_spec(client):
    r = await client.get("/api/catalog/postgresql")
    assert r.status == 200
    spec = await r.json()
    assert "host" in spec["config"]["properties"]
    assert spec["credentials_by_auth"][spec["auth"]["default"]]["properties"]["password"]["ui:type"] == "password"


async def test_catalog_unknown_type_404(client):
    r = await client.get("/api/catalog/nope")
    assert r.status == 404


async def test_index_served(client):
    r = await client.get("/")
    assert r.status == 200
    assert "text/html" in r.headers["Content-Type"]
    assert "Data Edge Agent" in await r.text()


async def test_audit_endpoint_returns_recent_entries(client):
    client.tunnel.audit.record(connection="lego-pg", operation="execute_query",
                               outcome="ok", duration_ms=9, row_count=3)
    r = await client.get("/api/audit?limit=50")
    assert r.status == 200
    entries = (await r.json())["entries"]
    assert len(entries) == 1
    assert entries[0]["operation"] == "execute_query" and entries[0]["row_count"] == 3

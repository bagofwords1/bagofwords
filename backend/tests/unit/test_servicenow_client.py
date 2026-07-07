"""Unit tests for ServiceNowClient.

Covers the client contract:
- test_connection: success, auth failure, and the silent metadata-ACL failure
  (HTTP 200 with an empty sys_dictionary result)
- get_schemas: curated set + `tables` override, field inheritance through the
  table hierarchy (incident extends task), reference fields -> foreign keys,
  sys_id primary key
- discover_all business-table filtering
- execute_query: JSON spec parsing, encoded-query/fields/display-value params,
  pagination, row cap, {link, value} normalization, malformed-spec errors

HTTP is faked at the `requests.Session` boundary and served from fixtures
captured from a real ServiceNow developer instance
(tests/unit/fixtures/servicenow/), so payload quirks — reference values as
{link, value} objects, dot-walked field keys — are the real thing.
"""
from __future__ import annotations

import json
import pathlib
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest

from app.data_sources.clients.servicenow_client import ServiceNowClient

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "servicenow"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    """Routes Table API GETs to canned responses and records every request."""

    def __init__(self, responder):
        self.responder = responder
        self.requests: list[tuple[str, dict]] = []
        self.auth = None
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        params = params or {}
        self.requests.append((urlparse(url).path, params))
        return self.responder(urlparse(url).path, params)

    def close(self):
        pass


@pytest.fixture
def client(monkeypatch):
    def make(responder, **kwargs):
        c = ServiceNowClient(
            instance_url="https://example.service-now.com",
            username="u",
            password="p",
            **kwargs,
        )
        session = FakeSession(responder)
        monkeypatch.setattr(
            "app.data_sources.clients.servicenow_client.requests.Session",
            lambda: session,
        )
        return c, session

    return make


def metadata_responder(path, params):
    """Serve the captured metadata fixtures like the real instance would."""
    if path.endswith("/table/sys_db_object"):
        return FakeResponse(load_fixture("sys_db_object.json"))
    if path.endswith("/table/sys_dictionary"):
        wanted = set()
        q = params.get("sysparm_query", "")
        for part in q.split("^"):
            if part.startswith("nameIN"):
                wanted = set(part[len("nameIN"):].split(","))
        rows = [
            r for r in load_fixture("sys_dictionary.json")["result"]
            if not wanted or r["name"] in wanted
        ]
        return FakeResponse({"result": rows})
    if path.endswith("/table/sys_user"):
        return FakeResponse({"result": [{"sys_id": "x"}]})
    raise AssertionError(f"unexpected path {path}")


# ── test_connection ──────────────────────────────────────────────────────────

def test_connection_success(client):
    c, _ = client(metadata_responder)
    result = c.test_connection()
    assert result["success"] is True


def test_connection_bad_credentials(client):
    c, _ = client(lambda path, params: FakeResponse({}, status_code=401))
    result = c.test_connection()
    assert result["success"] is False
    assert "401" in result["message"]


def test_connection_detects_silent_metadata_acl_failure(client):
    """Under-privileged users get HTTP 200 + empty result from sys_dictionary;
    the connection test must fail actionably instead of passing."""

    def responder(path, params):
        if path.endswith("/table/sys_dictionary"):
            return FakeResponse({"result": []})
        return FakeResponse({"result": [{"sys_id": "x"}]})

    c, _ = client(responder)
    result = c.test_connection()
    assert result["success"] is False
    assert "sys_dictionary" in result["message"]


# ── schema discovery ─────────────────────────────────────────────────────────

def test_get_schemas_inherits_parent_fields(client):
    c, _ = client(metadata_responder, tables="incident")
    tables = c.get_schemas()
    assert [t.name for t in tables] == ["incident"]
    incident = tables[0]
    col_names = {col.name for col in incident.columns}
    # own field
    assert "incident_state" in col_names
    # inherited from task (parent table)
    task_fields = {
        r["element"] for r in load_fixture("sys_dictionary.json")["result"]
        if r["name"] == "task"
    }
    assert task_fields & col_names == task_fields
    # pk is always sys_id
    assert [pk.name for pk in incident.pks] == ["sys_id"]


def test_get_schemas_reference_fields_become_fks(client):
    c, _ = client(metadata_responder, tables="incident")
    incident = c.get_schemas()[0]
    fk_map = {fk.column.name: fk.references_name for fk in incident.fks}
    assert fk_map, "reference fields should map to fks"
    # every fk references a table by name and points at sys_id
    for fk in incident.fks:
        assert fk.references_name
        assert fk.references_column.name == "sys_id"
    # a known reference captured in the fixture: incidents are assigned to users
    assert fk_map.get("assigned_to") == "sys_user"


def test_get_schemas_raises_on_empty_dictionary(client):
    def responder(path, params):
        if path.endswith("/table/sys_dictionary"):
            return FakeResponse({"result": []})
        return metadata_responder(path, params)

    c, _ = client(responder, tables="incident")
    with pytest.raises(RuntimeError, match="metadata read"):
        c.get_schemas()


def test_discover_all_filters_to_business_tables():
    c = ServiceNowClient(instance_url="https://x", username="u", password="p", discover_all=True)
    hierarchy = {
        "task": {"label": "Task", "parent": None},
        "incident": {"label": "Incident", "parent": "task"},
        "u_custom_orders": {"label": "Orders", "parent": None},
        "x_vendor_app_data": {"label": "Vendor", "parent": None},
        "cmdb_ci": {"label": "CI", "parent": None},
        "cmdb_ci_server": {"label": "Server", "parent": "cmdb_ci"},
        "sys_trigger": {"label": "Trigger", "parent": None},
        "sys_user": {"label": "User", "parent": None},
        "v_transaction": {"label": "Txn", "parent": None},
    }
    result = set(c._target_tables(hierarchy))
    assert {"task", "incident", "u_custom_orders", "x_vendor_app_data",
            "cmdb_ci", "cmdb_ci_server", "sys_user"} <= result
    assert "sys_trigger" not in result
    assert "v_transaction" not in result


def test_tables_config_overrides_default():
    c = ServiceNowClient(
        instance_url="https://x", username="u", password="p",
        tables="incident, sys_user ,u_custom",
    )
    assert c._target_tables({}) == ["incident", "sys_user", "u_custom"]


# ── execute_query ────────────────────────────────────────────────────────────

def test_execute_query_returns_dataframe_with_display_values(client):
    page = load_fixture("incident_page.json")

    def responder(path, params):
        assert path.endswith("/table/incident")
        assert params["sysparm_display_value"] == "true"
        assert params["sysparm_query"] == "active=true"
        assert "number" in params["sysparm_fields"]
        return FakeResponse(page)

    c, _ = client(responder)
    df = c.execute_query(json.dumps({
        "table": "incident",
        "query": "active=true",
        "fields": ["number", "short_description", "priority", "state"],
        "limit": 50,
    }))
    assert isinstance(df, pd.DataFrame)
    assert len(df) == len(page["result"])
    assert "number" in df.columns
    # display values are scalars, not {link, value} dicts
    assert not df.map(lambda v: isinstance(v, dict)).any().any()


def test_execute_query_normalizes_reference_objects(client):
    raw = load_fixture("incident_raw.json")
    assert any(isinstance(v, dict) for row in raw["result"] for v in row.values()), \
        "fixture should contain {link, value} reference objects"

    c, _ = client(lambda path, params: FakeResponse(raw), display_values=False)
    df = c.execute_query('{"table": "incident", "limit": 5}')
    assert not df.map(lambda v: isinstance(v, dict)).any().any()


def test_execute_query_paginates_until_limit(client):
    calls = []

    def responder(path, params):
        calls.append(params)
        n = int(params["sysparm_limit"])
        start = int(params["sysparm_offset"])
        rows = [{"number": f"INC{start + i}"} for i in range(n)]
        return FakeResponse({"result": rows})

    c, _ = client(responder)
    df = c.execute_query('{"table": "incident", "limit": 2500}')
    assert len(df) == 2500
    # pages advance by offset and never exceed the page size
    offsets = [int(p["sysparm_offset"]) for p in calls]
    assert offsets == sorted(set(offsets))
    assert all(int(p["sysparm_limit"]) <= 1000 for p in calls)


def test_execute_query_stops_on_short_page(client):
    def responder(path, params):
        offset = int(params["sysparm_offset"])
        rows = [{"number": "INC1"}] if offset == 0 else []
        return FakeResponse({"result": rows})

    c, _ = client(responder)
    df = c.execute_query('{"table": "incident", "limit": 5000}')
    assert len(df) == 1


def test_execute_query_caps_limit(client):
    seen = {}

    def responder(path, params):
        seen["limit"] = params["sysparm_limit"]
        return FakeResponse({"result": []})

    c, _ = client(responder)
    c.execute_query('{"table": "incident", "limit": 999999}')
    assert int(seen["limit"]) <= 10_000


@pytest.mark.parametrize("bad", ["SELECT * FROM incident", "{}", '{"query": "active=true"}', ""])
def test_execute_query_rejects_specs_without_table(client, bad):
    c, _ = client(lambda path, params: FakeResponse({"result": []}))
    with pytest.raises(ValueError):
        c.execute_query(bad)


def test_execute_query_accepts_dict_spec(client):
    c, _ = client(lambda path, params: FakeResponse({"result": [{"a": 1}]}))
    df = c.execute_query({"table": "incident", "limit": 1})
    assert len(df) == 1


def test_http_errors_surface_with_status(client):
    c, _ = client(lambda path, params: FakeResponse({}, status_code=403))
    with pytest.raises(RuntimeError, match="403"):
        c.execute_query('{"table": "incident"}')

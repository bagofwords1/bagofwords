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


# ── auth ─────────────────────────────────────────────────────────────────────

def test_connect_uses_basic_auth_by_default(client):
    c, session = client(metadata_responder)
    c.test_connection()
    assert session.auth == ("u", "p")
    assert "Authorization" not in session.headers


def test_connect_prefers_bearer_token_over_basic_auth(client):
    """Per-user OAuth: access_token wins even when service-account
    username/password are also present on the connection."""
    c, session = client(metadata_responder, access_token="tok-123")
    c.test_connection()
    assert session.headers["Authorization"] == "Bearer tok-123"
    assert session.auth is None


def test_connect_without_any_credentials_raises():
    c = ServiceNowClient(instance_url="https://example.service-now.com")
    with pytest.raises(RuntimeError, match="no credentials"):
        with c.connect():
            pass


def test_expired_oauth_token_message_mentions_sign_in(client):
    c, _ = client(lambda path, params: FakeResponse({}, status_code=401), access_token="tok-123")
    result = c.test_connection()
    assert result["success"] is False
    assert "sign in" in result["message"]


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


# ── attachments (file capabilities) ──────────────────────────────────────────
#
# Metadata lives in sys_attachment (served via the Attachment API); the binary
# comes back from /api/now/attachment/{sys_id}/file. The fake session here
# accepts the `headers` kwarg the binary download passes, which the metadata-
# only FakeSession above does not.

import pytest as _pytest  # noqa: E402  (kept local to this section)


class AttachmentSession:
    """Serves Attachment API metadata + binary, records requests."""

    def __init__(self, meta_rows, binary=b"%PDF-1.4 fake"):
        self.meta_rows = meta_rows
        self.binary = binary
        self.requests = []
        self.auth = None
        self.headers = {}

    def get(self, url, params=None, timeout=None, headers=None):
        path = urlparse(url).path
        self.requests.append((path, params or {}))

        class _Resp:
            def __init__(self, payload=None, content=None, status_code=200):
                self._payload = payload
                self.content = content if content is not None else b""
                self.status_code = status_code
                self.text = json.dumps(payload) if payload is not None else ""

            def json(self):
                return self._payload

        if path.endswith("/file"):
            return _Resp(content=self.binary)
        # /api/now/attachment/{sys_id}  (single) vs  /api/now/attachment (list)
        tail = path.rsplit("/api/now/attachment", 1)[-1].strip("/")
        if tail and "/" not in tail:
            match = next((r for r in self.meta_rows if r["sys_id"] == tail), None)
            return _Resp({"result": match})
        # list/query
        q = (params or {}).get("sysparm_query", "")
        rows = self.meta_rows
        if "file_nameLIKE" in q:
            needle = q.split("file_nameLIKE", 1)[1].split("^", 1)[0]
            rows = [r for r in rows if needle.lower() in r["file_name"].lower()]
        if "table_sys_id=" in q:
            rec = q.split("table_sys_id=", 1)[1].split("^", 1)[0]
            rows = [r for r in rows if r["table_sys_id"] == rec]
        if "file_name=" in q and "file_nameLIKE" not in q:
            fn = q.split("file_name=", 1)[1].split("^", 1)[0]
            rows = [r for r in rows if r["file_name"] == fn]
        return _Resp({"result": rows})

    def close(self):
        pass


PDF_ROW = {
    "sys_id": "a" * 32,
    "file_name": "vpn_setup_guide.pdf",
    "content_type": "application/pdf",
    "size_bytes": "973",
    "table_name": "kb_knowledge",
    "table_sys_id": "rec123",
    "sys_updated_on": "2026-09-03 18:00:00",
}
TXT_ROW = {
    "sys_id": "b" * 32,
    "file_name": "notes.txt",
    "content_type": "text/plain",
    "size_bytes": "12",
    "table_name": "kb_knowledge",
    "table_sys_id": "rec123",
    "sys_updated_on": "2026-09-03 18:05:00",
}


@_pytest.fixture
def attach_client(monkeypatch):
    def make(meta_rows, binary=b"%PDF-1.4 fake", **kwargs):
        c = ServiceNowClient(
            instance_url="https://example.service-now.com",
            username="u", password="p", **kwargs,
        )
        session = AttachmentSession(meta_rows, binary=binary)
        monkeypatch.setattr(
            "app.data_sources.clients.servicenow_client.requests.Session",
            lambda: session,
        )
        return c, session
    return make


def test_capabilities_include_file_tools_when_enabled():
    from app.data_sources.clients.base import Capability
    c = ServiceNowClient(instance_url="https://x", username="u", password="p")
    assert Capability.LIST_FILES in c.capabilities
    assert Capability.READ_FILE in c.capabilities
    assert Capability.SEARCH_FILES in c.capabilities
    assert c.cheap_live_listing is True


def test_capabilities_query_only_when_attachments_disabled():
    from app.data_sources.clients.base import Capability
    c = ServiceNowClient(instance_url="https://x", username="u", password="p",
                         enable_attachments=False)
    assert c.capabilities == {Capability.QUERY}


def test_list_files_returns_attachment_entries(attach_client):
    c, _ = attach_client([PDF_ROW, TXT_ROW])
    files = c.list_files()
    names = {f["name"] for f in files}
    assert names == {"vpn_setup_guide.pdf", "notes.txt"}
    pdf = next(f for f in files if f["name"] == "vpn_setup_guide.pdf")
    assert pdf["id"] == "a" * 32
    assert pdf["mime_type"] == "application/pdf"
    assert pdf["size"] == 973
    assert pdf["path"] == "kb_knowledge/rec123/vpn_setup_guide.pdf"
    assert pdf["is_folder"] is False


def test_list_files_scopes_to_configured_tables(attach_client):
    c, session = attach_client([PDF_ROW])
    c.list_files()
    _, params = session.requests[-1]
    assert params["sysparm_query"] == "table_nameINkb_knowledge"


def test_list_files_record_scope(attach_client):
    c, session = attach_client([PDF_ROW])
    c.list_files(folder_id="kb_knowledge/rec123")
    _, params = session.requests[-1]
    assert "table_name=kb_knowledge" in params["sysparm_query"]
    assert "table_sys_id=rec123" in params["sysparm_query"]


def test_list_files_disabled_raises(attach_client):
    c, _ = attach_client([PDF_ROW], enable_attachments=False)
    with pytest.raises(RuntimeError, match="disabled"):
        c.list_files()


def test_read_file_pdf_returns_extracted_text(attach_client, monkeypatch):
    # Stub the document extractor so the test doesn't depend on a real PDF.
    monkeypatch.setattr(
        "app.data_sources.clients.servicenow_client.extract_document_text_from_bytes",
        lambda content, name: "Corporate VPN Setup Guide\nInstall the client.",
    )
    monkeypatch.setattr(
        "app.data_sources.clients.servicenow_client.doc_text_is_usable",
        lambda text, ext: True,
    )
    c, _ = attach_client([PDF_ROW], binary=b"%PDF-1.4 realbytes")
    payload = c.read_file("a" * 32)
    assert isinstance(payload, str)
    assert "Corporate VPN Setup Guide" in payload
    # DocumentText carries the original name + bytes for the viewer/vision path.
    assert payload.name == "vpn_setup_guide.pdf"
    assert payload.raw == b"%PDF-1.4 realbytes"


def test_read_file_scanned_pdf_falls_back_to_named_bytes(attach_client, monkeypatch):
    monkeypatch.setattr(
        "app.data_sources.clients.servicenow_client.extract_document_text_from_bytes",
        lambda content, name: "",
    )
    monkeypatch.setattr(
        "app.data_sources.clients.servicenow_client.doc_text_is_usable",
        lambda text, ext: False,
    )
    c, _ = attach_client([PDF_ROW], binary=b"%PDF-1.4 scan")
    payload = c.read_file("a" * 32)
    assert isinstance(payload, bytes)
    assert payload.name == "vpn_setup_guide.pdf"
    assert payload.mime == "application/pdf"


def test_read_file_text(attach_client):
    c, _ = attach_client([TXT_ROW], binary=b"hello ticket")
    payload = c.read_file("b" * 32)
    assert payload == "hello ticket"


def test_read_file_resolves_bare_name(attach_client):
    c, _ = attach_client([TXT_ROW], binary=b"hello ticket")
    payload = c.read_file("notes.txt")
    assert payload == "hello ticket"


def test_read_file_resolves_path_id(attach_client, monkeypatch):
    monkeypatch.setattr(
        "app.data_sources.clients.servicenow_client.extract_document_text_from_bytes",
        lambda content, name: "text",
    )
    monkeypatch.setattr(
        "app.data_sources.clients.servicenow_client.doc_text_is_usable",
        lambda text, ext: True,
    )
    c, _ = attach_client([PDF_ROW], binary=b"x")
    payload = c.read_file("kb_knowledge/rec123/vpn_setup_guide.pdf")
    assert payload.name == "vpn_setup_guide.pdf"


def test_read_raw_bytes(attach_client):
    c, _ = attach_client([PDF_ROW], binary=b"%PDF-1.4 raw")
    content, name, mime = c.read_raw_bytes("a" * 32)
    assert content == b"%PDF-1.4 raw"
    assert name == "vpn_setup_guide.pdf"
    assert mime == "application/pdf"


def test_search_files_matches_filename(attach_client):
    c, session = attach_client([PDF_ROW, TXT_ROW])
    hits = c.search_files("vpn")
    assert [h["name"] for h in hits] == ["vpn_setup_guide.pdf"]
    _, params = session.requests[-1]
    assert "file_nameLIKEvpn" in params["sysparm_query"]
    assert "table_nameINkb_knowledge" in params["sysparm_query"]


def test_file_version_from_metadata(attach_client):
    c, _ = attach_client([PDF_ROW])
    assert c.file_version("a" * 32) == "2026-09-03 18:00:00|973"


def test_missing_attachment_raises(attach_client):
    c, _ = attach_client([PDF_ROW])
    with pytest.raises(ValueError, match="not found"):
        c.read_file("c" * 32)

"""Unit tests for QlikSenseOnPremClient — all QRS + QIX I/O is mocked.

The fixtures below are redacted copies of a real ``qlik_dump.json`` taken from a
Qlik Sense Enterprise 31.62.0.0 site: same key names, same nesting, same
oddities (an app whose model never loaded, master dimensions whose field
definitions come back empty, a key field linking five tables). Hostnames, GUIDs
and credentials are synthetic.
"""

import json
import os
import stat
import sys
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from app.data_sources.clients.qlik_sense_onprem_client import (
    QlikSenseOnPremClient,
    _mask_secrets,
)


# ---------------------------------------------------------------------------
# Certificate material — a throwaway self-signed pair so `load_cert_chain`
# (which only accepts file paths) has something real to load.
# ---------------------------------------------------------------------------


def _make_selfsigned():
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bow-test")])
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    encrypted_key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"hunter2"),
    ).decode()
    return cert_pem, key_pem, encrypted_key_pem


CERT_PEM, KEY_PEM, ENCRYPTED_KEY_PEM = _make_selfsigned()


def make_client(**overrides) -> QlikSenseOnPremClient:
    kwargs: Dict[str, Any] = {
        "server_url": "https://qlik.corp.example.com",
        "client_cert": CERT_PEM,
        "client_key": KEY_PEM,
        "verify_ssl": False,
    }
    kwargs.update(overrides)
    return QlikSenseOnPremClient(**kwargs)


# ---------------------------------------------------------------------------
# QRS fixtures
# ---------------------------------------------------------------------------

ABOUT = {"buildVersion": "31.62.0.0", "buildDate": "5/8/2026 10:00:00 AM", "requiresBootstrap": False}

STREAMS = [
    {"id": "str-everyone", "name": "Everyone", "createdDate": "2026-05-08T10:00:00.000Z"},
    {"id": "str-monitoring", "name": "Monitoring apps", "createdDate": "2026-05-08T10:00:00.000Z"},
    {"id": "str-finance", "name": "Finance", "createdDate": "2026-05-09T08:00:00.000Z"},
    {
        "id": "str-sales",
        "name": "Sales",
        "createdDate": "2026-05-09T08:00:00.000Z",
        "modifiedDate": "2026-05-09T08:00:00.000Z",
        "owner": {"userDirectory": "EXAMPLECORP", "userId": "dana", "name": "Dana Q"},
        "tags": [{"id": "tag-1", "name": "Governed"}],
        "customProperties": [
            {"id": "cp-1", "value": "Sales", "definition": {"id": "cpd-1", "name": "Department"}},
        ],
    },
]

APPS = [
    {
        "id": "app-consumer-sales",
        "name": "Consumer Sales",
        "description": "Retail sales analysis",
        "stream": {"id": "str-sales", "name": "Sales"},
        "published": True,
        "publishTime": "2026-05-10T09:00:00.000Z",
        "lastReloadTime": "2026-08-01T02:00:00.000Z",
        "createdDate": "2026-05-09T08:00:00.000Z",
        "modifiedDate": "2026-08-01T02:05:00.000Z",
        "modifiedByUserName": "EXAMPLECORP\\dana",
        "owner": {"userDirectory": "EXAMPLECORP", "userId": "dana", "name": "Dana Q"},
        "fileSize": 52428800,
        "availabilityStatus": 1,
        "tags": [{"id": "tag-1", "name": "Certified"}],
        "customProperties": [
            {"id": "cp-9", "value": "Production", "definition": {"id": "cpd-9", "name": "Environment"}},
        ],
    },
    {
        "id": "app-content-monitor",
        "name": "Content Monitor",
        "stream": {"id": "str-monitoring", "name": "Monitoring apps"},
        "published": True,
        "publishTime": "2026-05-08T10:30:00.000Z",
        # Never reloaded — this is the app that returns an empty data model.
        "lastReloadTime": None,
        "modifiedDate": "2026-05-08T10:30:00.000Z",
        "owner": {"userDirectory": "INTERNAL", "userId": "sa_repository", "name": "sa_repository"},
    },
    {
        "id": "app-financial-analysis",
        "name": "Financial Analysis",
        "stream": {"id": "str-finance", "name": "Finance"},
        "published": True,
        "publishTime": "2026-05-11T09:00:00.000Z",
        "lastReloadTime": "2026-07-30T02:00:00.000Z",
        "modifiedDate": "2026-07-30T02:10:00.000Z",
        "owner": {"userDirectory": "EXAMPLECORP", "userId": "morgan", "name": "Morgan R"},
    },
    {
        # Still in a personal work area — no stream at all.
        "id": "app-procurement-draft",
        "name": "Procurement(1)",
        "stream": None,
        "published": False,
        "publishTime": None,
        "lastReloadTime": "2026-07-29T02:00:00.000Z",
        "modifiedDate": "2026-07-29T02:00:00.000Z",
        "owner": {"userDirectory": "EXAMPLECORP", "userId": "alex", "name": "Alex P"},
    },
]

# /qrs/dataconnection/full hands back credentials in the clear.
DATA_CONNECTIONS = [
    {
        "id": "conn-1",
        "name": "monitor_apps_REST_operations_monitor",
        "type": "QvRestConnector.exe",
        "architecture": 0,
        "connectionstring": "CUSTOM CONNECT TO \"provider=QvRestConnector.exe;url=https://qlik.corp.example.com/qrs/app\"",
        "username": "EXAMPLECORP\\administrator",
        "password": "%%password%SuperSecret!123",
    },
    {
        "id": "conn-2",
        "name": "Sales QVDs",
        "type": "folder",
        "architecture": 0,
        "connectionstring": "D:\\QlikData\\QVD\\",
        "username": "",
        "password": "",
    },
]


# ---------------------------------------------------------------------------
# Engine fixtures
# ---------------------------------------------------------------------------

CONSUMER_SALES_QTR = [
    {
        "qName": "Sales",
        "qNoOfRows": 148000,
        "qLoose": False,
        "qComment": "Fact table, one row per order line",
        "qPos": {"qx": 120, "qy": 40},
        "qFields": [
            {
                "qName": "Order Number",
                "qTags": ["$key", "$numeric", "$integer"],
                "qnRows": 148000,
                "qnNonNulls": 148000,
                "qnTotalDistinctValues": 25000,
                "qnPresentDistinctValues": 25000,
                "qInformationDensity": 1,
                "qSubsetRatio": 1,
                "qKeyType": "PERFECT_KEY",
            },
            {
                "qName": "Sales Quantity",
                "qTags": ["$numeric", "$integer"],
                "qnRows": 148000,
                "qnNonNulls": 147800,
                "qnTotalDistinctValues": 96,
                "qInformationDensity": 0.9986,
                "qComment": "Units sold on the line",
            },
            {"qName": "Sales Price", "qTags": ["$numeric"], "qnRows": 148000},
            # A synthetic-key participant: no tags at all, density -1.
            {"qName": "%KeyRegionDate", "qTags": [], "qInformationDensity": -1,
             "qnNonNulls": 0, "qIsSynthetic": True},
        ],
    },
    {
        "qName": "Orders",
        "qNoOfRows": 25000,
        "qLoose": False,
        "qFields": [
            {"qName": "Order Number", "qTags": ["$key", "$numeric", "$integer"]},
            {"qName": "Order Date", "qTags": ["$date", "$numeric"]},
            {"qName": "Customer Number", "qTags": ["$key", "$numeric", "$integer"]},
        ],
    },
    {
        "qName": "Customers",
        "qNoOfRows": 3000,
        "qLoose": False,
        "qFields": [
            {"qName": "Customer Number", "qTags": ["$key", "$numeric", "$integer"]},
            {"qName": "Region Name", "qTags": ["$text", "$ascii"]},
            {"qName": "$Internal Load Ts", "qTags": ["$timestamp", "$hidden"]},
        ],
    },
]

CONSUMER_SALES_QK = [
    {"qKeyFields": ["Order Number"], "qTables": ["Sales", "Orders"]},
    {"qKeyFields": ["Customer Number"], "qTables": ["Orders", "Customers"]},
]

CONSUMER_SALES_MEASURES = [
    {
        "qInfo": {"qId": "meas-1", "qType": "measure"},
        "qMeta": {"title": "Northeast Revenue"},
        "qData": {
            "title": "Northeast Revenue",
            "description": "Revenue for the Northeast region only",
            "expression": 'Sum({$<[Region Name]={"Northeast"}>} [Sales Quantity]*[Sales Price])',
            "label": "NE Revenue",
        },
    },
    {
        "qInfo": {"qId": "meas-2", "qType": "measure"},
        "qMeta": {"title": "Total Quantity"},
        "qData": {
            "title": "Total Quantity",
            "description": "",
            "expression": "Sum([Sales Quantity])",
            "label": "",
        },
    },
]

# Master dimensions come back with `fields` as an empty string, not a list —
# qDimensionListDef's qData cannot resolve /qDim/qFieldDefs.
CONSUMER_SALES_DIMENSIONS = [
    {
        "qInfo": {"qId": "dim-1", "qType": "dimension"},
        "qMeta": {"title": "Region"},
        "qData": {"title": "Region", "description": "Sales region", "grouping": "N", "fields": ""},
    },
    {
        "qInfo": {"qId": "dim-2", "qType": "dimension"},
        "qMeta": {"title": "Product Hierarchy"},
        "qData": {"title": "Product Hierarchy", "description": "", "grouping": "H", "fields": ""},
    },
]

CONSUMER_SALES_VARIABLES = [
    {"qInfo": {"qId": "var-1"}, "qName": "vCurrency", "qData": {"definition": "'USD'"}},
    {
        "qInfo": {"qId": "var-2"},
        "qName": "vApiEndpoint",
        "qData": {"definition": "'https://api.example.com/v1?apikey=abc123def'"},
    },
]

CONSUMER_SALES_SHEETS = [
    {
        "qInfo": {"qId": "sheet-1", "qType": "sheet"},
        "qMeta": {"title": "Revenue Overview"},
        "qData": {"title": "Revenue Overview", "description": "Top-line revenue by region and month"},
    },
    {
        "qInfo": {"qId": "sheet-2", "qType": "sheet"},
        "qMeta": {"title": "Product Detail"},
        "qData": {"title": "Product Detail", "description": ""},
    },
]

CONSUMER_SALES_LINEAGE = [
    {"qDiscriminator": "lib://Sales QVDs/sales.qvd", "qStatement": ""},
    {"qDiscriminator": "lib://Sales QVDs/orders.qvd", "qStatement": ""},
    # Duplicated in the real dump — the same QVD is loaded twice.
    {"qDiscriminator": "lib://Sales QVDs/sales.qvd", "qStatement": ""},
    {
        "qDiscriminator": "Finance DB",
        "qStatement": "SELECT * FROM dbo.Ledger WHERE Year >= 2024;Password=SuperSecret!123",
    },
    {"qDiscriminator": "", "qStatement": "RESIDENT TempSales"},
]


class _CannedEngine:
    """Replays one app's Engine responses in the order the client asks for them."""

    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        self.methods: List[str] = []

    async def rpc(self, handle, method, params):
        self.methods.append(method)
        if "raise_on" in self.payload and self.payload["raise_on"] == method:
            raise RuntimeError(self.payload.get("raise_msg") or "boom")
        if method == "OpenDoc":
            return {"qReturn": {"qHandle": 1}}
        if method == "GetAppLayout":
            return {"qLayout": {"qHasData": self.payload.get("has_data")}}
        if method == "GetTablesAndKeys":
            return {"qtr": self.payload.get("qtr") or [], "qk": self.payload.get("qk") or []}
        if method == "CreateSessionObject":
            qtype = (params[0].get("qInfo") or {}).get("qType")
            self._pending = qtype
            return {"qReturn": {"qHandle": 2}}
        if method == "GetLayout":
            key = {
                "MeasureList": ("qMeasureList", "measures"),
                "DimensionList": ("qDimensionList", "dimensions"),
                "VariableList": ("qVariableList", "variables"),
                "SheetList": ("qAppObjectList", "sheets"),
            }[self._pending]
            return {"qLayout": {key[0]: {"qItems": self.payload.get(key[1]) or []}}}
        if method == "GetLineage":
            return self.payload.get("lineage") or []
        if method == "GetScript":
            return {"qScript": "x" * (self.payload.get("script_chars") or 0)}
        raise AssertionError(f"unexpected Engine method {method}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def install_engine(monkeypatch, client, payloads: Dict[str, Dict[str, Any]]):
    """Route each app id to its canned Engine responses."""
    engines: Dict[str, _CannedEngine] = {}

    def _session(app_id, user):
        engines[app_id] = _CannedEngine(payloads[app_id])
        engines[app_id].user = user
        return engines[app_id]

    monkeypatch.setattr(client, "_qix_session", _session)
    return engines


def install_qrs(client, routes: Dict[str, Any]):
    """Answer `_qrs_get` from a dict, recording the calls."""
    calls: List[Dict[str, Any]] = []

    def _get(path, params=None, user=None):
        calls.append({"path": path, "params": params, "user": user})
        if path not in routes:
            raise RuntimeError(f"unexpected QRS path {path}")
        return routes[path]

    client._qrs_get = _get  # type: ignore[method-assign]
    return calls


CONSUMER_SALES_PAYLOAD = {
    "has_data": True,
    "has_script": True,
    "file_name": "Consumer Sales",
    "state_names": ["Compare"],
    "section_access": True,
    "script_excerpt": "SECTION ACCESS;\nLOAD * INLINE [ACCESS, USERID];",
    "sheet_names": ["Revenue Overview", "Product Detail"],
    "lineage_sources": ["lib://Sales QVDs/sales.qvd", "lib://Sales QVDs/orders.qvd", "Finance DB"],
    "qtr": CONSUMER_SALES_QTR,
    "qk": CONSUMER_SALES_QK,
    "measures": CONSUMER_SALES_MEASURES,
    "dimensions": CONSUMER_SALES_DIMENSIONS,
    "variables": CONSUMER_SALES_VARIABLES,
    "sheets": CONSUMER_SALES_SHEETS,
    "lineage": CONSUMER_SALES_LINEAGE,
    "script_chars": 18000,
}

# Content Monitor: a published app whose script has never run. No tables, no
# keys, no qHasData — but 100+ master measures.
CONTENT_MONITOR_PAYLOAD = {
    "has_data": None,
    "qtr": [],
    "qk": [],
    "measures": CONSUMER_SALES_MEASURES,
    "dimensions": [],
    "variables": [],
    "sheets": [],
    "lineage": [],
    "script_chars": 0,
}

EMPTY_PAYLOAD = {
    "has_data": None, "qtr": [], "qk": [], "measures": [], "dimensions": [],
    "variables": [], "sheets": [], "lineage": [], "script_chars": 0,
}


# ---------------------------------------------------------------------------
# 1. Configuration and certificate material
# ---------------------------------------------------------------------------


class TestSetup:
    @pytest.mark.parametrize("raw,expected", [
        ("https://qlik.corp.example.com", "qlik.corp.example.com"),
        ("qlik.corp.example.com", "qlik.corp.example.com"),
        # The QMC/hub port must not leak into the QRS or Engine URLs.
        ("https://qlik.corp.example.com:443/qmc", "qlik.corp.example.com"),
        ("http://qlik.corp.example.com/", "qlik.corp.example.com"),
    ])
    def test_parse_host_strips_scheme_port_and_path(self, raw, expected):
        assert QlikSenseOnPremClient._parse_host(raw) == expected

    def test_missing_certificate_raises_an_actionable_error(self):
        client = QlikSenseOnPremClient(server_url="https://qlik.corp.example.com")
        with pytest.raises(RuntimeError, match="QMC"):
            client.connect()

    def test_cert_files_are_private_and_shredded_on_close(self):
        client = make_client()
        client.connect()
        cert_path, key_path = client._cert_path, client._key_path
        assert os.path.isfile(cert_path) and os.path.isfile(key_path)
        # 0600: a client certificate is admin-equivalent on a Qlik site.
        for path in (cert_path, key_path):
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        client.close()
        assert not os.path.exists(cert_path)
        assert not os.path.exists(key_path)

    def test_encrypted_key_is_decrypted_once_at_materialization(self):
        """`requests` cannot carry a key passphrase, so it is spent up front."""
        client = make_client(client_key=ENCRYPTED_KEY_PEM, client_key_password="hunter2")
        client.connect()
        with open(client._key_path) as fh:
            written = fh.read()
        assert "ENCRYPTED" not in written
        assert "BEGIN PRIVATE KEY" in written
        client.close()

    def test_wrong_key_password_is_reported_plainly(self):
        client = make_client(client_key=ENCRYPTED_KEY_PEM, client_key_password="wrong")
        with pytest.raises(RuntimeError, match="decrypt the Qlik client key"):
            client.connect()

    def test_root_ca_becomes_the_verify_target(self):
        client = make_client(root_ca=CERT_PEM, verify_ssl=True)
        client.connect()
        assert client._verify_arg() == client._ca_path
        client.close()

    def test_verify_arg_without_root_ca_is_the_flag(self):
        client = make_client(verify_ssl=False)
        client.connect()
        assert client._verify_arg() is False
        client.close()


# ---------------------------------------------------------------------------
# 2. The QRS request contract (xrfkey + X-Qlik-User)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"{}"
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class TestQrsRequestContract:
    def _capture(self, client, payload=None):
        seen: Dict[str, Any] = {}

        class _Session:
            def get(self_inner, url, **kwargs):
                seen["url"] = url
                seen.update(kwargs)
                return _FakeResponse(payload if payload is not None else {})

            def close(self_inner):
                pass

        client.connect()
        client._http = _Session()
        return seen

    def test_xrfkey_is_sent_in_both_the_query_string_and_the_header(self):
        """QRS rejects a request carrying only one of them, with an HTTP 400
        whose body never mentions xrfkey — so this is pinned."""
        client = make_client()
        seen = self._capture(client, ABOUT)
        client._qrs_get("about")

        assert seen["params"]["xrfkey"] == seen["headers"]["X-Qlik-Xrfkey"]
        assert len(seen["params"]["xrfkey"]) == 16
        client.close()

    def test_url_targets_the_qrs_port_and_prefix(self):
        client = make_client(qrs_port=4242)
        seen = self._capture(client, ABOUT)
        client._qrs_get("app/full")
        assert seen["url"] == "https://qlik.corp.example.com:4242/qrs/app/full"
        client.close()

    def test_client_certificate_is_presented(self):
        client = make_client()
        seen = self._capture(client, ABOUT)
        client._qrs_get("about")
        assert seen["cert"] == (client._cert_path, client._key_path)
        client.close()

    def test_acting_user_header_defaults_to_the_service_account(self):
        client = make_client()
        seen = self._capture(client, ABOUT)
        client._qrs_get("about")
        assert seen["headers"]["X-Qlik-User"] == "UserDirectory=INTERNAL; UserId=sa_repository"
        client.close()

    def test_http_error_body_is_surfaced(self):
        client = make_client()
        client.connect()

        class _Session:
            def get(self, url, **kwargs):
                resp = _FakeResponse({}, status_code=403)
                resp.text = "Forbidden"
                return resp

            def close(self):
                pass

        client._http = _Session()
        with pytest.raises(RuntimeError, match="HTTP 403"):
            client._qrs_get("app/full")
        client.close()


class TestActingUser:
    def test_explicit_user_wins_over_app_owner(self):
        """An admin who names an identity gets that identity — silently swapping
        in the app owner would sidestep the Section Access rules they chose."""
        client = make_client(user_directory="EXAMPLECORP", user_id="auditor",
                             impersonate_app_owner=True)
        acting = client._acting_user({"owner": {"userDirectory": "EXAMPLECORP", "userId": "dana"}})
        assert acting == "UserDirectory=EXAMPLECORP; UserId=auditor"

    def test_app_owner_is_used_when_no_identity_is_configured(self):
        client = make_client(impersonate_app_owner=True)
        acting = client._acting_user({"owner": {"userDirectory": "EXAMPLECORP", "userId": "dana"}})
        assert acting == "UserDirectory=EXAMPLECORP; UserId=dana"

    def test_impersonation_can_be_turned_off(self):
        client = make_client(impersonate_app_owner=False)
        acting = client._acting_user({"owner": {"userDirectory": "EXAMPLECORP", "userId": "dana"}})
        assert acting == "UserDirectory=INTERNAL; UserId=sa_repository"


# ---------------------------------------------------------------------------
# 3. Enumeration
# ---------------------------------------------------------------------------


class TestListApps:
    def test_unpublished_apps_are_skipped_by_default(self):
        client = make_client()
        install_qrs(client, {"app/full": APPS, "stream/full": STREAMS})
        names = [a["name"] for a in client.list_apps()]
        assert "Procurement(1)" not in names
        assert len(names) == 3

    def test_unpublished_apps_can_be_included(self):
        client = make_client(published_only=False)
        install_qrs(client, {"app/full": APPS, "stream/full": STREAMS})
        apps = {a["name"]: a for a in client.list_apps()}
        assert apps["Procurement(1)"]["space_name"] == ""

    def test_stream_filter_matches_name_or_id(self):
        for token in ("Sales", "str-sales"):
            client = make_client(stream_filter=token)
            install_qrs(client, {"app/full": APPS, "stream/full": STREAMS})
            assert [a["name"] for a in client.list_apps()] == ["Consumer Sales"]

    def test_apps_are_ordered_by_last_reload_so_max_apps_keeps_the_freshest(self):
        client = make_client(max_apps=2)
        install_qrs(client, {"app/full": APPS, "stream/full": STREAMS})
        assert [a["name"] for a in client.list_apps()] == ["Consumer Sales", "Financial Analysis"]

    def test_stream_is_carried_as_the_container(self):
        client = make_client()
        install_qrs(client, {"app/full": APPS, "stream/full": STREAMS})
        app = next(a for a in client.list_apps() if a["id"] == "app-consumer-sales")
        assert app["space_name"] == "Sales"
        assert app["space_id"] == "str-sales"
        assert client._app_key(app) == "Sales/Consumer Sales"


class TestDataConnections:
    def test_credentials_never_leave_the_client(self):
        """/qrs/dataconnection/full returns every connection's password in the
        clear. Only the name and type are allowed past this method."""
        client = make_client()
        install_qrs(client, {"dataconnection/full": DATA_CONNECTIONS})
        conns = client.list_data_connections()

        blob = json.dumps(conns)
        assert "SuperSecret" not in blob
        assert "administrator" not in blob
        assert "connectionstring" not in blob
        assert {c["name"] for c in conns} == {"monitor_apps_REST_operations_monitor", "Sales QVDs"}


class TestTestConnection:
    def test_reports_version_and_counts(self):
        client = make_client()
        install_qrs(client, {"about": ABOUT, "stream/full": STREAMS, "app/full": APPS})
        result = client.test_connection()
        assert result["success"] is True
        assert result["version"] == "31.62.0.0"
        assert result["stream_count"] == 4
        assert result["app_count"] == 3

    def test_reachable_but_empty_says_so_without_failing(self):
        client = make_client(stream_filter="Nonexistent")
        install_qrs(client, {"about": ABOUT, "stream/full": STREAMS, "app/full": APPS})
        result = client.test_connection()
        assert result["success"] is True
        assert "no apps are visible" in result["message"]

    def test_qrs_unreachable_is_a_failure(self):
        client = make_client()

        def _boom(path, params=None, user=None):
            raise RuntimeError("Connection refused")

        client._qrs_get = _boom
        result = client.test_connection()
        assert result["success"] is False
        assert "Repository Service" in result["message"]


# ---------------------------------------------------------------------------
# 4. Data-model mapping
# ---------------------------------------------------------------------------


APP_ROW = {
    "id": "app-consumer-sales", "name": "Consumer Sales",
    "description": "Retail sales analysis",
    "space_id": "str-sales", "space_name": "Sales",
    "published": True, "publish_time": "2026-05-10T09:00:00.000Z",
    "last_reload": "2026-08-01T02:00:00.000Z",
    "created_at": "2026-05-09T08:00:00.000Z",
    "updated_at": "2026-08-01T02:05:00.000Z",
    "modified_by": "EXAMPLECORP\\dana",
    "owner": {"userDirectory": "EXAMPLECORP", "userId": "dana", "name": "Dana Q"},
    "file_size": 52428800,
    "availability_status": 1,
    "tags": ["Certified"],
    "custom_properties": {"Environment": ["Production"]},
}


class TestBuildDataTables:
    def _tables(self, **kwargs):
        client = make_client(**kwargs)
        # Routed even where the test does not read streams — otherwise the
        # stream lookup inside _app_meta would attempt a real HTTP request.
        install_qrs(client, {"stream/full": STREAMS})
        return {
            t.name: t
            for t in client._build_data_tables(
                APP_ROW, CONSUMER_SALES_QTR, CONSUMER_SALES_QK, CONSUMER_SALES_PAYLOAD
            )
        }

    def test_tables_are_named_stream_app_table(self):
        assert set(self._tables()) == {
            "Sales/Consumer Sales/Sales",
            "Sales/Consumer Sales/Orders",
            "Sales/Consumer Sales/Customers",
        }

    def test_field_tags_become_dtypes(self):
        sales = self._tables()["Sales/Consumer Sales/Sales"]
        dtypes = {c.name: c.dtype for c in sales.columns}
        assert dtypes["Order Number"] == "key"
        assert dtypes["Sales Price"] == "numeric"
        # A synthetic-key participant carries no tags at all.
        assert dtypes["%KeyRegionDate"] == "unknown"

    def test_key_fields_become_primary_keys(self):
        orders = self._tables()["Sales/Consumer Sales/Orders"]
        assert {c.name for c in orders.pks} == {"Order Number", "Customer Number"}

    def test_associations_are_emitted_from_both_sides(self):
        """Qlik keys have no direction — `qk` says a field links a set of tables,
        so the edge has to exist on every table in the set."""
        tables = self._tables()
        sales_fks = {(f.column.name, f.references_name) for f in tables["Sales/Consumer Sales/Sales"].fks}
        orders_fks = {(f.column.name, f.references_name) for f in tables["Sales/Consumer Sales/Orders"].fks}
        assert ("Order Number", "Sales/Consumer Sales/Orders") in sales_fks
        assert ("Order Number", "Sales/Consumer Sales/Sales") in orders_fks
        assert ("Customer Number", "Sales/Consumer Sales/Customers") in orders_fks

    def test_qlik_system_fields_are_marked_hidden_but_kept(self):
        """Hidden is where join keys live, so the field stays queryable."""
        customers = self._tables()["Sales/Consumer Sales/Customers"]
        col = next(c for c in customers.columns if c.name == "$Internal Load Ts")
        assert col.metadata["hidden"] is True

    def test_metadata_uses_stream_keys_not_space_keys(self):
        meta = self._tables()["Sales/Consumer Sales/Sales"].metadata_json["qlik_sense_onprem"]
        assert meta["streamName"] == "Sales"
        assert meta["appId"] == "app-consumer-sales"
        assert meta["rowCount"] == 148000
        assert "spaceName" not in meta

    def test_field_statistics_are_kept(self):
        """Qlik profiles every field at reload. Cardinality and null density are
        the difference between knowing a column is groupable and querying to
        find out."""
        sales = self._tables()["Sales/Consumer Sales/Sales"]
        by_name = {c.name: c.metadata for c in sales.columns}
        order = by_name["Order Number"]
        assert order["rows"] == 148000
        assert order["distinctValues"] == 25000
        assert order["informationDensity"] == 1
        assert order["subsetRatio"] == 1
        assert order["keyType"] == "PERFECT_KEY"
        assert order["relationship_key"] is True
        qty = by_name["Sales Quantity"]
        assert qty["nonNulls"] == 147800
        assert qty["comment"] == "Units sold on the line"

    def test_a_field_comment_becomes_the_column_description(self):
        sales = self._tables()["Sales/Consumer Sales/Sales"]
        qty = next(c for c in sales.columns if c.name == "Sales Quantity")
        assert qty.description == "Units sold on the line"

    def test_zero_is_kept_but_absent_stats_are_dropped(self):
        """`qnNonNulls: 0` is the signal for a synthetic-key participant, so it
        must survive the compaction that drops empty values."""
        sales = self._tables()["Sales/Consumer Sales/Sales"]
        by_name = {c.name: c.metadata for c in sales.columns}
        synthetic = by_name["%KeyRegionDate"]
        assert synthetic["nonNulls"] == 0
        assert synthetic["informationDensity"] == -1
        assert synthetic["synthetic"] is True
        # Sales Price carries only qnRows — the unset stats are not stored as None.
        assert "distinctValues" not in by_name["Sales Price"]

    def test_table_level_extras_are_kept(self):
        table = self._tables()["Sales/Consumer Sales/Sales"]
        meta = table.metadata_json["qlik_sense_onprem"]
        assert meta["comment"] == "Fact table, one row per order line"
        assert meta["fieldCount"] == 4
        assert table.description == "Fact table, one row per order line"
        # Associations survive even where the fan-out cap suppresses the edges.
        assert {"fields": ["Order Number"], "tables": ["Orders"]} in meta["associations"]

    def test_app_level_metadata_is_stamped_on_every_table(self):
        """Power BI puts ids, owner, webUrl and its reports on each table; the
        same questions have to be answerable here without re-crawling."""
        for table in self._tables().values():
            meta = table.metadata_json["qlik_sense_onprem"]
            assert meta["owner"] == "EXAMPLECORP\\dana"
            assert meta["webUrl"] == "https://qlik.corp.example.com/sense/app/app-consumer-sales"
            assert meta["tags"] == ["Certified"]
            assert meta["customProperties"] == {"Environment": ["Production"]}
            assert meta["appDescription"] == "Retail sales analysis"
            assert meta["fileSizeBytes"] == 52428800
            assert meta["publishTime"] == "2026-05-10T09:00:00.000Z"
            assert meta["sheets"] == ["Revenue Overview", "Product Detail"]
            assert meta["lineageSources"][0] == "lib://Sales QVDs/sales.qvd"
            assert meta["alternateStates"] == ["Compare"]

    def test_section_access_is_flagged_like_power_bi_rls(self):
        meta = self._tables()["Sales/Consumer Sales/Sales"].metadata_json["qlik_sense_onprem"]
        assert meta["sectionAccess"] is True

    def test_stream_tags_and_custom_properties_are_folded_in(self):
        """A stream's governance labels describe every app published into it."""
        meta = self._tables()["Sales/Consumer Sales/Sales"].metadata_json["qlik_sense_onprem"]
        assert meta["streamTags"] == ["Governed"]
        assert meta["streamCustomProperties"] == {"Department": ["Sales"]}

    def test_hub_keys_are_recorded_instead_of_fanned_out(self):
        """A key linking many tables would emit n*(n-1) edges that say nothing a
        single note doesn't."""
        client = make_client()
        install_qrs(client, {"stream/full": STREAMS})
        app = {"id": "a", "name": "Wide", "space_name": "S", "space_id": "s"}
        wide_tables = [f"T{i}" for i in range(20)]
        qtr = [{"qName": t, "qNoOfRows": 1, "qFields": [{"qName": "Date", "qTags": ["$key"]}]} for t in wide_tables]
        qk = [{"qKeyFields": ["Date"], "qTables": wide_tables}]

        built = client._build_data_tables(app, qtr, qk)

        assert all(t.fks == [] for t in built)
        meta = built[0].metadata_json["qlik_sense_onprem"]
        assert meta["associativeHubKeys"] == ["Date"]


# ---------------------------------------------------------------------------
# 5. Master items
# ---------------------------------------------------------------------------


class TestMasterItems:
    def _master(self, payload=None, **kwargs):
        client = make_client(**kwargs)
        install_qrs(client, {"stream/full": STREAMS})
        return client._build_master_table(APP_ROW, payload or CONSUMER_SALES_PAYLOAD, set())

    def test_measures_carry_their_qlik_expression(self):
        """The expression is the whole point — set analysis cannot be recovered
        from a raw Sum() over the same fields."""
        table = self._master()
        measure = next(c for c in table.columns if c.name == "Northeast Revenue")
        assert measure.dtype == "measure"
        assert measure.metadata["role"] == "measure"
        assert measure.metadata["expression"] == (
            'Sum({$<[Region Name]={"Northeast"}>} [Sales Quantity]*[Sales Price])'
        )
        assert measure.metadata["label"] == "NE Revenue"

    def test_measure_without_a_description_falls_back_to_its_expression(self):
        table = self._master()
        measure = next(c for c in table.columns if c.name == "Total Quantity")
        assert measure.description == "Sum([Sales Quantity])"

    def test_a_human_description_does_not_displace_the_expression(self):
        """Column metadata is allowlisted on its way into the prompt and
        `expression` is not on the list, so `description` is the only field that
        reaches the model. A measure that has both must carry both."""
        table = self._master()
        measure = next(c for c in table.columns if c.name == "Northeast Revenue")
        assert "Revenue for the Northeast region only" in measure.description
        assert '{$<[Region Name]={"Northeast"}>}' in measure.description

    def test_sheets_are_captured_like_power_bi_dashboards(self):
        """Two levels of detail: `sheets` is a name-only list cheap enough to
        repeat on every table, `sheetDetails` carries the descriptions once."""
        meta = self._master().metadata_json["qlik_sense_onprem"]
        assert meta["sheets"] == ["Revenue Overview", "Product Detail"]
        by_name = {s["name"]: s for s in meta["sheetDetails"]}
        assert set(by_name) == {"Revenue Overview", "Product Detail"}
        assert by_name["Revenue Overview"]["description"] == "Top-line revenue by region and month"
        # An empty description is omitted rather than stored as "".
        assert "description" not in by_name["Product Detail"]

    def test_an_app_with_only_sheets_still_publishes(self):
        payload = {**EMPTY_PAYLOAD, "sheets": CONSUMER_SALES_SHEETS}
        table = self._master(payload=payload)
        assert table is not None
        assert len(table.metadata_json["qlik_sense_onprem"]["sheetDetails"]) == 2

    def test_drilldown_dimensions_are_flagged(self):
        table = self._master()
        dims = {c.name: c for c in table.columns if c.dtype == "dimension"}
        assert dims["Product Hierarchy"].metadata.get("drilldown") is True
        assert "drilldown" not in dims["Region"].metadata

    def test_variables_are_captured_with_secrets_masked(self):
        meta = self._master().metadata_json["qlik_sense_onprem"]
        by_name = {v["name"]: v["definition"] for v in meta["variables"]}
        assert by_name["vCurrency"] == "'USD'"
        assert "abc123def" not in by_name["vApiEndpoint"]

    def test_lineage_is_deduped_and_stripped_of_credentials(self):
        meta = self._master().metadata_json["qlik_sense_onprem"]
        lineage = meta["lineage"]
        sources = [entry.get("source") for entry in lineage]
        assert sources.count("lib://Sales QVDs/sales.qvd") == 1
        blob = json.dumps(lineage)
        assert "SuperSecret" not in blob
        assert "Password=***" in blob

    def test_counts_and_script_size_land_in_metadata(self):
        meta = self._master().metadata_json["qlik_sense_onprem"]
        assert meta["objectType"] == "master_items"
        assert meta["measureCount"] == 2
        assert meta["dimensionCount"] == 2
        assert meta["scriptChars"] == 18000
        assert meta["hasData"] is True

    def test_nothing_to_publish_yields_no_table(self):
        assert self._master(payload=EMPTY_PAYLOAD) is None

    def test_name_collision_with_a_real_table_is_disambiguated(self):
        client = make_client()
        install_qrs(client, {"stream/full": STREAMS})
        app = {"id": "app-1", "name": "Consumer Sales", "space_id": "s", "space_name": "Sales"}
        taken = {"Sales/Consumer Sales/Master Items"}
        table = client._build_master_table(app, CONSUMER_SALES_PAYLOAD, taken)
        assert table.name == "Sales/Consumer Sales/Master Items (app-1)"


class TestMasterItemsReachThePrompt:
    """The crawl is only useful if what it extracts survives into the context.

    `_COLUMN_META_KEYS` in tables_schema_section is an allowlist and the LAST
    gate before the model — a key absent from it never renders, no matter what
    discovery captured. Asserting on the built `Table` alone would have passed
    while the agent saw nothing, so this renders the real context.
    """

    def _rendered(self, monkeypatch):
        from app.ai.context.sections.tables_schema_section import TablesSchemaContext
        from app.schemas.data_source_schema import DataSourceSummarySchema

        client = make_client()
        install_qrs(client, {"app/full": [APPS[0]], "stream/full": STREAMS})
        install_engine(monkeypatch, client, {"app-consumer-sales": CONSUMER_SALES_PAYLOAD})
        tables = client.get_schemas()

        ctx = TablesSchemaContext(data_sources=[
            TablesSchemaContext.DataSource(
                info=DataSourceSummarySchema(id="1", name="Qlik", type="qlik_sense_onprem"),
                tables=tables,
            )
        ])
        return ctx.render_combined(top_k_per_ds=50, index_limit=200)

    def test_measure_expressions_render(self, monkeypatch):
        out = self._rendered(monkeypatch)
        assert "Sum([Sales Quantity])" in out
        assert "[Sales Quantity]*[Sales Price]" in out

    def test_set_analysis_quotes_are_escaped_not_left_to_break_the_attribute(self, monkeypatch):
        """Qlik set analysis routinely contains `"`, and every rendered value
        sits inside a double-quoted XML attribute — an unescaped quote closes it
        early and the rest of the expression is read as stray attributes."""
        out = self._rendered(monkeypatch)
        assert '{$&lt;[Region Name]={&quot;Northeast&quot;}&gt;}' in out
        assert '{"Northeast"}' not in out

    def test_measures_render_with_a_measure_role(self, monkeypatch):
        out = self._rendered(monkeypatch)
        assert 'role="measure"' in out
        assert 'name="Northeast Revenue"' in out

    def test_the_data_model_renders_alongside_them(self, monkeypatch):
        out = self._rendered(monkeypatch)
        assert "Sales/Consumer Sales/Orders" in out
        assert 'name="Order Number"' in out


def test_mask_secrets_covers_the_common_assignment_shapes():
    masked = _mask_secrets("Driver=x;Password=p@ss;uid=bob;api_key = k3y;token=t0k")
    assert "p@ss" not in masked
    assert "k3y" not in masked
    assert "t0k" not in masked
    assert "uid=bob" in masked


# ---------------------------------------------------------------------------
# 6. Whole-app crawl
# ---------------------------------------------------------------------------


class TestCrawl:
    def test_full_app_yields_data_tables_plus_master_items(self, monkeypatch):
        client = make_client()
        install_qrs(client, {"app/full": [APPS[0]], "stream/full": STREAMS})
        install_engine(monkeypatch, client, {"app-consumer-sales": CONSUMER_SALES_PAYLOAD})

        tables = client.get_schemas()

        names = {t.name for t in tables}
        assert "Sales/Consumer Sales/Sales" in names
        assert "Sales/Consumer Sales/Master Items" in names
        assert all(t.is_active for t in tables)

    def test_app_with_no_data_model_still_publishes_its_master_items(self, monkeypatch):
        """Content Monitor has never been reloaded but carries 100+ measures —
        losing them because the model is empty would be the wrong trade."""
        client = make_client()
        install_qrs(client, {"app/full": [APPS[1]], "stream/full": STREAMS})
        install_engine(monkeypatch, client, {"app-content-monitor": CONTENT_MONITOR_PAYLOAD})

        tables = client.get_schemas()

        assert [t.name for t in tables] == ["Monitoring apps/Content Monitor/Master Items"]
        assert tables[0].metadata_json["qlik_sense_onprem"].get("hasData") is None

    def test_a_truly_empty_app_is_recorded_not_invented(self, monkeypatch):
        """No phantom column-less table: an unreloaded app is an inactive row
        with `status: empty`, which is a state, not an error."""
        client = make_client()
        install_qrs(client, {"app/full": [APPS[1]], "stream/full": STREAMS})
        install_engine(monkeypatch, client, {"app-content-monitor": EMPTY_PAYLOAD})

        tables = client.get_schemas()

        assert len(tables) == 1
        assert tables[0].is_active is False
        assert tables[0].columns == []
        assert tables[0].metadata_json["qlik_sense_onprem"]["status"] == "empty"

    def test_one_bad_app_does_not_sink_the_site(self, monkeypatch):
        client = make_client()
        install_qrs(client, {"app/full": [APPS[0], APPS[2]], "stream/full": STREAMS})
        install_engine(monkeypatch, client, {
            "app-consumer-sales": CONSUMER_SALES_PAYLOAD,
            "app-financial-analysis": {**EMPTY_PAYLOAD, "raise_on": "OpenDoc",
                                       "raise_msg": "App access denied"},
        })

        tables = client.get_schemas()

        errored = [t for t in tables if (t.metadata_json or {}).get("qlik_sense_onprem", {}).get("status") == "error"]
        assert len(errored) == 1
        assert errored[0].is_active is False
        assert "App access denied" in errored[0].description
        assert any(t.name == "Sales/Consumer Sales/Sales" for t in tables)

    def test_each_app_is_opened_as_its_owner(self, monkeypatch):
        client = make_client()
        install_qrs(client, {"app/full": [APPS[0]], "stream/full": STREAMS})
        engines = install_engine(monkeypatch, client, {"app-consumer-sales": CONSUMER_SALES_PAYLOAD})

        client.get_schemas()

        assert engines["app-consumer-sales"].user == "UserDirectory=EXAMPLECORP; UserId=dana"

    def test_master_items_can_be_turned_off(self, monkeypatch):
        client = make_client(include_master_items=False, include_lineage=False)
        install_qrs(client, {"app/full": [APPS[0]], "stream/full": STREAMS})
        engines = install_engine(monkeypatch, client, {"app-consumer-sales": CONSUMER_SALES_PAYLOAD})

        tables = client.get_schemas()

        assert not any("Master Items" in t.name for t in tables)
        assert "GetLineage" not in engines["app-consumer-sales"].methods
        assert "CreateSessionObject" not in engines["app-consumer-sales"].methods

    def test_get_schema_finds_a_table_by_its_full_name(self, monkeypatch):
        client = make_client()
        install_qrs(client, {"app/full": [APPS[0]], "stream/full": STREAMS})
        install_engine(monkeypatch, client, {"app-consumer-sales": CONSUMER_SALES_PAYLOAD})
        assert client.get_schema("Sales/Consumer Sales/Orders").name == "Sales/Consumer Sales/Orders"

    def test_get_schema_falls_back_to_the_bare_table_name(self, monkeypatch):
        client = make_client()
        install_qrs(client, {"app/full": [APPS[0]], "stream/full": STREAMS})
        install_engine(monkeypatch, client, {"app-consumer-sales": CONSUMER_SALES_PAYLOAD})
        assert client.get_schema("Orders").name == "Sales/Consumer Sales/Orders"


# ---------------------------------------------------------------------------
# 7. App resolution
# ---------------------------------------------------------------------------


class TestResolveApp:
    def test_a_guid_short_circuits_the_listing(self):
        client = make_client()

        def _boom(*a, **k):
            raise AssertionError("list_apps() must not be called for a GUID")

        client.list_apps = _boom
        app_id, row = client._resolve_app("11111111-2222-3333-4444-555555555555")
        assert app_id == "11111111-2222-3333-4444-555555555555"
        assert row is None

    @pytest.mark.parametrize("target", [
        "Sales/Consumer Sales",
        "Sales/Consumer Sales/Orders",
        "Consumer Sales",
        "app-consumer-sales",
    ])
    def test_all_the_shapes_a_table_name_can_arrive_in(self, target):
        client = make_client()
        install_qrs(client, {"app/full": APPS, "stream/full": STREAMS})
        app_id, row = client._resolve_app(target)
        assert app_id == "app-consumer-sales"
        assert row is not None

    def test_an_unknown_target_is_passed_through(self):
        """The caller may hold an id the listing cannot see — let Qlik decide."""
        client = make_client()
        install_qrs(client, {"app/full": APPS, "stream/full": STREAMS})
        app_id, row = client._resolve_app("Nope")
        assert app_id == "Nope"
        assert row is None


# ---------------------------------------------------------------------------
# 8. Query execution over the real transport
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    def __init__(self, canned: List[dict]):
        self._canned = list(canned)
        self.sent: List[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, raw: str):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if not self._canned:
            raise AssertionError("FakeWebSocket ran out of canned responses")
        return json.dumps(self._canned.pop(0))


class _StrictFakeConnect:
    """Enforces the real `websockets` 14+ signature.

    An unknown kwarg is not a clean error in the library — it falls through
    `**kwargs` into `loop.create_connection()`. A permissive fake is how
    `extra_headers=` shipped broken, so this one refuses to be permissive.
    """

    def __init__(self, canned: List[dict]):
        self.canned = canned
        self.calls: List[dict] = []

    def __call__(self, uri, *, additional_headers=None, ssl=None,
                 open_timeout=None, close_timeout=None, **unknown):
        if unknown:
            raise TypeError(
                "BaseEventLoop.create_connection() got an unexpected keyword "
                f"argument '{next(iter(unknown))}'"
            )
        self.calls.append({"uri": uri, "additional_headers": additional_headers, "ssl": ssl})
        self.ws = _FakeWebSocket(list(self.canned))
        return self.ws


def _install_ws(monkeypatch, connect):
    fake = MagicMock()
    fake.connect = connect
    monkeypatch.setitem(sys.modules, "websockets", fake)


CUBE_CANNED = [
    {"jsonrpc": "2.0", "method": "OnConnected", "params": {"qSessionState": "SESSION_CREATED"}},
    {"jsonrpc": "2.0", "id": 1, "result": {"qReturn": {"qHandle": 1}}},      # OpenDoc
    {"jsonrpc": "2.0", "id": 2, "result": {"qReturn": {"qHandle": 2}}},      # CreateSessionObject
    {"jsonrpc": "2.0", "id": 3, "result": {"qDataPages": [{"qMatrix": [
        [{"qText": "Northeast", "qNum": "NaN"}, {"qText": "12,500", "qNum": 12500}],
        [{"qText": "Midwest", "qNum": "NaN"}, {"qText": "8,100", "qNum": 8100}],
    ]}]}},
]


class TestExecuteQuery:
    def test_hypercube_round_trip(self, monkeypatch):
        client = make_client()
        install_qrs(client, {"app/full": APPS, "stream/full": STREAMS})
        connect = _StrictFakeConnect(CUBE_CANNED)
        _install_ws(monkeypatch, connect)

        df = client.execute_query(
            app="Sales/Consumer Sales",
            dimensions=["Region Name"],
            measures=[{"expr": "Sum([Sales Quantity])", "alias": "Qty"}],
            max_rows=100,
        )

        assert list(df.columns) == ["Region Name", "Qty"]
        assert df.iloc[0]["Qty"] == 12500
        client.close()

    def test_socket_targets_the_engine_port_and_names_the_acting_user(self, monkeypatch):
        """Cert auth authenticates the service, not a person — X-Qlik-User is
        what makes Qlik apply that user's Section Access."""
        client = make_client(user_directory="EXAMPLECORP", user_id="dana")
        install_qrs(client, {"app/full": APPS, "stream/full": STREAMS})
        connect = _StrictFakeConnect([
            {"jsonrpc": "2.0", "id": 1, "result": {"qReturn": {"qHandle": 1}}},
            {"jsonrpc": "2.0", "id": 2, "result": {"qReturn": {"qHandle": 2}}},
            {"jsonrpc": "2.0", "id": 3, "result": {"qDataPages": [{"qMatrix": [
                [{"qText": "176,000", "qNum": 176000}],
            ]}]}},
        ])
        _install_ws(monkeypatch, connect)

        client.execute_query(app="Sales/Consumer Sales", measures=[{"expr": "Count(1)"}])

        call = connect.calls[0]
        assert call["uri"] == "wss://qlik.corp.example.com:4747/app/app-consumer-sales"
        assert list(call["additional_headers"]) == [
            ("X-Qlik-User", "UserDirectory=EXAMPLECORP; UserId=dana")
        ]
        # Mutual TLS: the context must carry the client certificate.
        assert call["ssl"] is not None
        client.close()

    def test_filters_are_applied_as_selections_before_the_cube(self, monkeypatch):
        """A Qlik selection narrows the associative state for every linked
        table, which a post-hoc filter on the result would not."""
        client = make_client()
        install_qrs(client, {"app/full": APPS, "stream/full": STREAMS})
        connect = _StrictFakeConnect([
            {"jsonrpc": "2.0", "id": 1, "result": {"qReturn": {"qHandle": 1}}},
            {"jsonrpc": "2.0", "id": 2, "result": {"qReturn": {"qChangedHandles": []}}},
            {"jsonrpc": "2.0", "id": 3, "result": {"qReturn": {"qHandle": 2}}},
            {"jsonrpc": "2.0", "id": 4, "result": {"qDataPages": [{"qMatrix": []}]}},
        ])
        _install_ws(monkeypatch, connect)

        client.execute_query(
            app="app-consumer-sales",
            dimensions=["Region Name"],
            filters={"Order Date": ["2026-01-01"]},
        )

        methods = [m["method"] for m in connect.ws.sent]
        assert methods[:3] == ["OpenDoc", "SelectInField", "CreateSessionObject"]
        client.close()

    def test_a_cube_with_neither_dimensions_nor_measures_is_rejected(self):
        client = make_client()
        with pytest.raises(ValueError, match="At least one dimension or measure"):
            client.execute_query(app="Sales/Consumer Sales")

    def test_app_is_required(self):
        client = make_client()
        with pytest.raises(ValueError, match="app"):
            client.execute_query(dimensions=["Region Name"])


# ---------------------------------------------------------------------------
# 9. Registry wiring
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_the_type_resolves_to_this_client(self):
        from app.schemas.data_source_registry import REGISTRY, resolve_client_class

        entry = REGISTRY["qlik_sense_onprem"]
        assert entry.category == "bi"
        assert resolve_client_class("qlik_sense_onprem") is QlikSenseOnPremClient

    def test_the_certificate_variant_is_offered_to_both_scopes(self):
        from app.schemas.data_source_registry import REGISTRY

        variant = REGISTRY["qlik_sense_onprem"].credentials_auth.by_auth["certificate"]
        assert set(variant.scopes) == {"system", "user"}
        fields = variant.schema.model_fields
        assert fields["client_cert"].is_required()
        assert fields["client_key"].is_required()
        # The acting identity is per-credential, which is what lets a user-scoped
        # connection be evaluated as that user.
        assert "user_id" in fields

    def test_config_and_credential_fields_match_the_constructor(self):
        """The registry schemas ARE the connection form, and connection_service
        narrows what it passes to the constructor's signature — a field that is
        not a parameter is silently dropped instead of taking effect."""
        import inspect

        from app.schemas.data_source_registry import REGISTRY

        entry = REGISTRY["qlik_sense_onprem"]
        params = set(inspect.signature(QlikSenseOnPremClient.__init__).parameters) - {"self"}
        declared = set(entry.config_schema.model_fields) | set(
            entry.credentials_auth.by_auth["certificate"].schema.model_fields
        )
        assert declared <= params, f"not accepted by the client: {sorted(declared - params)}"

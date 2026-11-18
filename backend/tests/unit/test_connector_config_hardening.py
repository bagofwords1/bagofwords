"""Connector configuration cannot reach past the connection it describes.

Contracts:

* every field that names a destination gets the outbound check, whatever the
  field is called for that connector;
* free-form option fields accept tuning and authentication keys, never keys
  that repoint the connection, load code, or read a local file;
* a connector runs each query on a connection of its own.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.core import egress
from app.core.egress import check_connection_params, connection_hosts
from app.core.url_safety import UnsafeURLError

METADATA = "169.254.169.254"


@pytest.fixture(autouse=True)
def _network(monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(egress, "block_private_outbound", lambda: False)
    table = {"imds.corp.invalid": METADATA, "internal.corp.invalid": "10.1.2.3"}

    def fake_dns(host):
        if host in table:
            import socket

            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], 0))]
        raise OSError("unresolvable in tests")

    monkeypatch.setattr(egress, "_getaddrinfo", fake_dns)


# ── destination fields ──────────────────────────────────────────────────────

KUBE_ACCESS_FILE = """apiVersion: v1
kind: Config
clusters:
- name: prod
  cluster:
    server: https://{host}:6443
    certificate-authority-data: AAAA
users:
- name: reader
  user:
    token: abc
"""

# (connector type, params with {host} where the destination goes)
DESTINATION_FIELDS = [
    ("cosmosdb", {"endpoint": "https://{host}:443/"}),
    ("couchbase", {"connection_string": "couchbases://other.example.test,{host}?timeout=5"}),
    ("iceberg", {"catalog_type": "rest", "uri": "https://{host}/api/catalog"}),
    ("iceberg", {"catalog_type": "rest", "uri": "https://catalog.example.test", "s3_endpoint": "http://{host}:9000"}),
    ("iceberg", {"uri": "https://catalog.example.test", "oauth2_server_uri": "https://{host}/token"}),
    ("mongodb", {"host": "m1.example.test:27017,{host}:27017", "port": 27017, "database": "d"}),
    ("azure_blob", {"account_url": "https://{host}/account"}),
    ("azure_blob", {"connection_string": "DefaultEndpointsProtocol=http;AccountName=a;AccountKey=k;BlobEndpoint=http://{host}:10000/a;"}),
    ("kubernetes", {"access_file": KUBE_ACCESS_FILE}),
]


def _fill(params, host):
    return {k: v.replace("{host}", host) if isinstance(v, str) else v for k, v in params.items()}


@pytest.mark.parametrize("conn_type, params", DESTINATION_FIELDS)
@pytest.mark.parametrize("host", [METADATA, "imds.corp.invalid"])
def test_metadata_addresses_are_refused_in_every_destination_field(conn_type, params, host):
    with pytest.raises(UnsafeURLError):
        check_connection_params(conn_type, _fill(params, host))


@pytest.mark.parametrize("conn_type, params", DESTINATION_FIELDS)
def test_private_destinations_follow_the_operator_s_setting(monkeypatch, conn_type, params):
    filled = _fill(params, "internal.corp.invalid")
    assert "internal.corp.invalid" in connection_hosts(conn_type, filled)
    check_connection_params(conn_type, filled)  # allowed by default
    monkeypatch.setattr(egress, "block_private_outbound", lambda: True)
    with pytest.raises(UnsafeURLError):
        check_connection_params(conn_type, filled)


@pytest.mark.parametrize("conn_type, params", DESTINATION_FIELDS)
def test_ordinary_destinations_pass(conn_type, params):
    check_connection_params(conn_type, _fill(params, "service.example.test"))


def test_azure_connection_string_with_a_custom_suffix_is_checked_as_a_host():
    hosts = connection_hosts("azure_blob", {
        "connection_string": "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=k;EndpointSuffix=corp.invalid"})
    assert hosts == ["acct.blob.corp.invalid"]


@pytest.mark.parametrize("account, ok", [
    ("ABCDEF-GHIJKL", True),
    ("xy12345.eu-west-1", True),
    ("acme_org-prod.privatelink", True),
    ("imds.corp.invalid/#", False),
    ("acct@other.example.test", False),
    ("acct:8443", False),
])
def test_snowflake_account_is_an_identifier_not_an_address(account, ok):
    params = {"account": account, "warehouse": "w", "database": "d"}
    if ok:
        check_connection_params("snowflake", params)
    else:
        with pytest.raises(UnsafeURLError):
            check_connection_params("snowflake", params)


# ── Iceberg extra properties ────────────────────────────────────────────────

def _iceberg(extra):
    from app.data_sources.clients.iceberg_client import IcebergClient

    return IcebergClient(catalog_type="rest", uri="https://catalog.example.test", warehouse="wh",
                         token="t", extra_properties=json.dumps(extra))


@pytest.mark.parametrize("key, value", [
    ("py-catalog-impl", "some.module.Catalog"),
    ("py-io-impl", "some.module.FileIO"),
    ("PY-IO-IMPL", "os.system"),
    ("py-io-impl", "pyiceberg_evil.io.FileIO"),
    ("s3.retry-strategy-impl", "some.module.Retry"),
    ("py-location-provider.impl", "some.module.Provider"),
    ("auth", {"type": "custom", "impl": "some.module.Auth"}),
])
def test_iceberg_extra_properties_refuse_classes_from_outside_pyiceberg(key, value):
    with pytest.raises(ValueError) as exc:
        _iceberg({key: value})._properties()
    assert key in str(exc.value)


@pytest.mark.parametrize("key, value", [
    ("py-io-impl", "pyiceberg.io.fsspec.FsspecFileIO"),
    ("py-io-impl", "pyiceberg.io.pyarrow.PyArrowFileIO"),
    ("py-catalog-impl", "pyiceberg.catalog.sql.SqlCatalog"),
    ("auth", {"type": "basic", "basic": {"username": "u", "password": "p"}}),
])
def test_iceberg_built_in_implementations_keep_working(key, value):
    assert _iceberg({key: value})._properties()[key] == str(value)


def test_iceberg_extra_properties_stay_free_form():
    """Catalog type, warehouse, credentials, endpoints and tuning all pass through."""
    extra = {
        "type": "glue", "warehouse": "s3://bucket/wh", "credential": "id:secret",
        "s3.region": "eu-west-1", "s3.connect-timeout": 30, "adls.account-name": "acme",
        "header.X-Custom": "1", "scope": "catalog", "s3.endpoint": "https://minio.example.test:9000",
        "client.profile-name": "analytics", "some.future-option": "x",
    }
    props = _iceberg(extra)._properties()
    for key, value in extra.items():
        assert props[key] == str(value)


@pytest.mark.parametrize("key", ["uri", "s3.endpoint", "glue.endpoint", "oauth2-server-uri", "gcs.service.host", "s3.signer.uri"])
def test_iceberg_endpoint_properties_get_the_outbound_check(monkeypatch, key):
    with pytest.raises(ValueError) as exc:
        _iceberg({key: "http://imds.corp.invalid/latest"})._properties()
    assert key in str(exc.value)

    # A private endpoint (MinIO on the LAN) is the operator's call.
    assert _iceberg({key: "http://internal.corp.invalid:9000"})._properties()[key]
    monkeypatch.setattr(egress, "block_private_outbound", lambda: True)
    with pytest.raises(ValueError):
        _iceberg({key: "http://internal.corp.invalid:9000"})._properties()


def test_iceberg_sql_catalog_file_follows_the_local_path_policy(tmp_path):
    ok = {"type": "sql", "uri": f"sqlite:///{tmp_path}/catalog.db", "warehouse": f"file://{tmp_path}"}
    assert _iceberg(ok)._properties()["uri"] == ok["uri"]

    for extra in ({"type": "sql", "uri": "sqlite:////etc/passwd"},
                  {"type": "sql", "uri": "sqlite:////etc/ssl/x.db"},
                  {"warehouse": "file:///etc"}):
        with pytest.raises(ValueError) as exc:
            _iceberg(extra)._properties()
        assert "uri" in str(exc.value) or "warehouse" in str(exc.value)


# ── SQL Server additional parameters ────────────────────────────────────────

def _odbc_keywords(client):
    from urllib.parse import unquote_plus

    odbc = unquote_plus(client.sql_server_uri).split("odbc_connect=", 1)[1]
    return {part.split("=", 1)[0].lower(): part.split("=", 1)[1] for part in odbc.split(";") if part}


@pytest.mark.parametrize("key", ["FILEDSN", "SaveFile", "AttachDBFileName", "filedsn"])
def test_mssql_additional_params_cannot_touch_files_on_the_server(key):
    from app.data_sources.clients.mssql_client import MSSQLClient

    kw = _odbc_keywords(MSSQLClient("sql.example.test", 1433, "db", user="u", password="p",
                                    additional_params={key: "/tmp/x"}))
    assert key.lower() not in kw


def test_mssql_mirroring_partner_keeps_working_and_gets_the_outbound_check(monkeypatch):
    from app.data_sources.clients.mssql_client import MSSQLClient

    extra = {"FailoverPartner": "tcp:internal.corp.invalid,1433", "ServerSPN": "MSSQLSvc/sql.example.test:1433",
             "DSN": "corp", "ServerCertificate": "/etc/ssl/certs/sql.pem"}
    kw = _odbc_keywords(MSSQLClient("sql.example.test", 1433, "db", user="u", password="p", additional_params=extra))
    for key, value in extra.items():
        assert kw[key.lower()] == value

    params = {"host": "sql.example.test", "port": 1433, "additional_params": extra}
    assert connection_hosts("MSSQL", params) == ["sql.example.test", "internal.corp.invalid"]
    check_connection_params("MSSQL", params)  # private partner: allowed by default
    monkeypatch.setattr(egress, "block_private_outbound", lambda: True)
    with pytest.raises(UnsafeURLError):
        check_connection_params("MSSQL", params)
    monkeypatch.setattr(egress, "block_private_outbound", lambda: False)
    for key in ("FailoverPartner", "Addr", "Address"):
        with pytest.raises(UnsafeURLError):
            check_connection_params("MSSQL", {"host": "sql.example.test", "additional_params": {key: "imds.corp.invalid\\inst"}})


def test_mssql_service_name_cannot_be_overridden_under_per_user_delegation():
    from app.data_sources.clients.mssql_client import MSSQLClient

    kw = _odbc_keywords(MSSQLClient(
        "sql.example.test", 1433, "db", use_kerberos=True, kerberos_principal="svc@REALM",
        kerberos_impersonate="alice@REALM", additional_params={"ServerSPN": "HTTP/other.example.test"}))
    assert "serverspn" not in kw


def test_mssql_additional_params_keep_ordinary_options():
    from urllib.parse import unquote_plus

    from app.data_sources.clients.mssql_client import MSSQLClient

    odbc = unquote_plus(MSSQLClient(
        "sql.example.test", 1433, "db", user="u", password="p",
        additional_params={"ApplicationIntent": "ReadOnly", "MultiSubnetFailover": "Yes", "ConnectRetryCount": 3},
    ).sql_server_uri)
    assert "ApplicationIntent=ReadOnly;" in odbc and "MultiSubnetFailover=Yes;" in odbc
    assert "ConnectRetryCount=3;" in odbc
    assert "SERVER=sql.example.test,1433;" in odbc


# ── Vertica: one connection per call ────────────────────────────────────────

class _FakeVertica:
    """Stands in for the vertica-python driver: each connection answers with
    the identity it was opened with."""

    def __init__(self):
        self.opened, self.closed = [], []

    def connect(self, **kwargs):
        driver = self
        driver.opened.append(kwargs)

        class _Cursor:
            description = None

            def execute(self, sql):
                self.sql = sql
                self.description = [("who",), ("amount",)]

            def fetchall(self):
                return [[kwargs["user"] + "@" + kwargs["host"], Decimal("1.50")]]

        class _Conn:
            def cursor(self):
                return _Cursor()

            def close(self):
                driver.closed.append(kwargs)

        return _Conn()


def test_vertica_clients_never_share_a_connection(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from app.data_sources.clients import vertica_client

    driver = _FakeVertica()
    monkeypatch.setattr(vertica_client, "vertica_python", driver)
    a = vertica_client.VerticaClient(host="vertica-a.example.test", port=5433, database="d", user="org_a", password="pa")
    b = vertica_client.VerticaClient(host="vertica-b.example.test", port=5433, database="d", user="org_b", password="pb")

    def ask(client):
        return client.execute_query("select current_user() as who, 1.5 as amount;")["who"].tolist()

    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(ask, [a, b] * 20))

    assert answers == [["org_a@vertica-a.example.test"], ["org_b@vertica-b.example.test"]] * 20
    # A connection per query, each closed, each opened with its own client's identity.
    assert len(driver.opened) == 40 and len(driver.closed) == 40
    assert {(o["host"], o["user"], o["password"]) for o in driver.opened} == {
        ("vertica-a.example.test", "org_a", "pa"), ("vertica-b.example.test", "org_b", "pb")}
    assert a.execute_query("select 1.5 as amount")["amount"].tolist() == [1.5]
    assert a.test_connection()["success"] is True

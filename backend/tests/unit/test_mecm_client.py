"""Configuration Manager (SCCM / MECM) connector, driven against the mock AdminService.

Configuration Manager needs Windows Server, SQL Server and Active Directory, so
there is no container or SaaS sandbox. `tools/mecm/mock_adminservice.py` stands
in at the protocol level and is served here over real HTTPS (a self-signed
certificate, like the one a site binds by default) with a real NTLM handshake
(pyspnego acceptor). That keeps the test hermetic while exercising what only
breaks over the wire: NTLM, certificate trust, CSDL parsing, nextLink paging,
query-option round-trips and error classification.
"""
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from app.data_sources.clients.mecm_client import MecmClient, normalize_base_url, parse_metadata

ADMIN = ("CONTOSO\\svc_bow", "Passw0rd!")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def mock():
    """The mock AdminService module (estate, request log, knobs)."""
    saved = os.environ.get("NTLM_USER_FILE")
    tools = Path(__file__).resolve().parents[3] / "tools" / "mecm"
    sys.path.insert(0, str(tools))
    try:
        import mock_adminservice  # noqa: E402  (sets NTLM_USER_FILE for its acceptor)
        yield mock_adminservice
    finally:
        sys.path.remove(str(tools))
        if saved is None:
            os.environ.pop("NTLM_USER_FILE", None)
        else:
            os.environ["NTLM_USER_FILE"] = saved


@pytest.fixture(scope="module")
def server(mock, tmp_path_factory):
    """Serve the mock over HTTPS; yield (host:port, certificate PEM)."""
    cert, key = mock.ensure_cert(tmp_path_factory.mktemp("mecm-cert"))
    port = _free_port()
    config = uvicorn.Config(mock.app, host="127.0.0.1", port=port, log_level="error",
                            ssl_certfile=str(cert), ssl_keyfile=str(key))
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    if not srv.started:
        pytest.fail("mock AdminService did not start")
    yield f"localhost:{port}", cert.read_text()
    srv.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def client(server):
    host, pem = server
    return MecmClient(server_url=host, username=ADMIN[0], password=ADMIN[1], ca_certificate=pem)


def _total(mock, name):
    return len(mock.CLASSES[name]["rows"]())


# ── URL handling ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "cm01.contoso.com",
    "https://cm01.contoso.com",
    "https://cm01.contoso.com/",
    "https://cm01.contoso.com/AdminService",
    "https://cm01.contoso.com/adminservice/wmi/",
    "https://CM01.contoso.com/AdminService/v1.0/Device",
])
def test_server_url_normalizes_to_the_adminservice_root(raw):
    assert normalize_base_url(raw).lower() == "https://cm01.contoso.com/adminservice"
    assert normalize_base_url(raw).endswith("/AdminService")


def test_server_url_keeps_a_gateway_prefix_and_port():
    url = "https://contoso.cloudapp.net:8443/CCM_Proxy_MutualAuth/72186325152220500/AdminService/wmi"
    assert normalize_base_url(url) == "https://contoso.cloudapp.net:8443/CCM_Proxy_MutualAuth/72186325152220500/AdminService"


@pytest.mark.parametrize("raw", [
    "", "http://cm01.contoso.com", "https://user:pw@cm01.contoso.com",
    "https://cm01.contoso.com/AdminService?x=1", "https://cm01.contoso.com/../x",
])
def test_server_url_rejects_unsafe_or_plain_http_values(raw):
    with pytest.raises(ValueError):
        normalize_base_url(raw)


def test_ntlm_needs_both_user_and_password():
    with pytest.raises(ValueError):
        MecmClient(server_url="cm01.contoso.com", username="CONTOSO\\svc", password="")


def test_pasted_certificate_must_be_pem():
    with pytest.raises(ValueError):
        MecmClient(server_url="cm01.contoso.com", username="a", password="b", ca_certificate="not a cert")


# ── connection test ─────────────────────────────────────────────────────────

def test_connection_succeeds_and_names_the_site(client, mock):
    result = client.test_connection()
    assert result["success"] is True
    site = mock.CLASSES["SMS_Site"]["rows"]()[0]
    assert site["SiteCode"] in result["message"]


@pytest.mark.parametrize("username", ["CONTOSO\\svc_bow", "svc_bow@contoso.lab"])
def test_connection_accepts_both_windows_user_name_forms(server, username):
    host, pem = server
    assert MecmClient(server_url=host, username=username, password=ADMIN[1], ca_certificate=pem).test_connection()["success"]


def test_connection_reports_bad_credentials_as_an_auth_failure(server):
    host, pem = server
    result = MecmClient(server_url=host, username=ADMIN[0], password="wrong", ca_certificate=pem).test_connection()
    assert result["success"] is False
    assert "401" in result["message"]


def test_connection_explains_an_account_that_is_not_a_cm_administrator(server):
    host, pem = server
    result = MecmClient(server_url=host, username="CONTOSO\\notadmin", password="Passw0rd!", ca_certificate=pem).test_connection()
    assert result["success"] is False
    assert "403" in result["message"] and "administrative user" in result["message"]


def test_untrusted_self_signed_certificate_fails_with_tls_guidance(server):
    host, _pem = server
    result = MecmClient(server_url=host, username=ADMIN[0], password=ADMIN[1]).test_connection()
    assert result["success"] is False
    assert "certificate" in result["message"].lower()


def test_verification_can_be_turned_off_for_a_lab(server):
    host, _pem = server
    with pytest.warns(Warning):
        assert MecmClient(server_url=host, username=ADMIN[0], password=ADMIN[1], verify_ssl=False).test_connection()["success"]


def test_unreachable_host_is_reported_not_raised():
    result = MecmClient(server_url=f"localhost:{_free_port()}", username=ADMIN[0], password=ADMIN[1]).test_connection()
    assert result["success"] is False
    assert "reach" in result["message"]


# ── catalog ─────────────────────────────────────────────────────────────────

def test_default_catalog_is_the_curated_set_present_on_the_site(client, mock):
    names = [t.name for t in client.get_schemas()]
    assert names and set(names) <= set(mock.CLASSES)
    assert "SMS_R_System" in names and "SMS_FullCollectionMembership" in names
    assert len(names) < len(mock.CLASSES)  # curated, not everything


def test_every_catalog_table_has_typed_columns_and_keys(client, mock):
    for table in client.get_schemas():
        defined = dict(mock.CLASSES[table.name]["props"])
        assert {c.name for c in table.columns} == set(defined)
        assert all(c.dtype == defined[c.name] for c in table.columns)
        assert [p.name for p in table.pks] == mock.CLASSES[table.name]["key"]
        assert table.description


def test_device_classes_link_to_the_device_resource(client):
    tables = {t.name: t for t in client.get_schemas()}
    for name, table in tables.items():
        cols = {c.name.lower() for c in table.columns}
        if name != "SMS_R_System" and ("resourceid" in cols or "machineid" in cols):
            assert any(fk.references_name == "SMS_R_System" for fk in table.fks), name


def test_discover_all_indexes_every_wmi_class(server, mock):
    host, pem = server
    c = MecmClient(server_url=host, username=ADMIN[0], password=ADMIN[1], ca_certificate=pem, discover_all=True)
    names = {t.name for t in c.get_schemas()}
    assert set(mock.CLASSES) <= names
    assert {f"v1.0/{n}" for n in mock.V1} <= names


def test_explicit_class_list_is_case_and_route_insensitive(server):
    host, pem = server
    c = MecmClient(server_url=host, username=ADMIN[0], password=ADMIN[1], ca_certificate=pem,
                   classes="sms_r_system, wmi/SMS_Collection\nv1.0/Device; SMS_DoesNotExist")
    assert [t.name for t in c.get_schemas()] == ["SMS_R_System", "SMS_Collection", "v1.0/Device"]


def test_include_v1_adds_versioned_entities(server, mock):
    host, pem = server
    c = MecmClient(server_url=host, username=ADMIN[0], password=ADMIN[1], ca_certificate=pem, include_v1=True)
    names = {t.name for t in c.get_schemas()}
    assert {f"v1.0/{n}" for n in mock.V1} <= names
    assert "SMS_R_System" in names


def test_metadata_parser_follows_base_types():
    csdl = """<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx"><edmx:DataServices>
      <Schema Namespace="NS" Alias="n" xmlns="http://docs.oasis-open.org/odata/ns/edm">
        <EntityType Name="Base"><Key><PropertyRef Name="Id"/></Key><Property Name="Id" Type="Edm.Int32"/></EntityType>
        <EntityType Name="Child" BaseType="n.Base"><Property Name="Name" Type="Edm.String">
          <Annotation Term="Org.OData.Core.V1.Description" String="Display name"/></Property></EntityType>
        <EntityContainer Name="C"><EntitySet Name="Children" EntityType="NS.Child"/></EntityContainer>
      </Schema></edmx:DataServices></edmx:Edmx>"""
    parsed = parse_metadata(csdl)
    assert list(parsed) == ["Children"]
    assert parsed["Children"]["keys"] == ["Id"]
    assert [(p["name"], p["type"]) for p in parsed["Children"]["properties"]] == [("Id", "Edm.Int32"), ("Name", "Edm.String")]
    assert parsed["Children"]["properties"][1]["description"] == "Display name"


# ── queries ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["SMS_G_System_INSTALLED_SOFTWARE", "SMS_FullCollectionMembership", "SMS_R_System"])
def test_unbounded_read_returns_every_row_across_pages(client, mock, name):
    assert _total(mock, name) > 0
    assert len(client.execute_query(name)) == _total(mock, name)


@pytest.mark.parametrize("max_rows", [1, 37, 250, 100_000])
def test_max_rows_bounds_the_result(client, mock, max_rows):
    total = _total(mock, "SMS_G_System_INSTALLED_SOFTWARE")
    assert len(client.execute_query("SMS_G_System_INSTALLED_SOFTWARE", max_rows=max_rows)) == min(max_rows, total)


def test_filter_and_select_are_pushed_to_the_server(client, mock):
    df = client.execute_query("SMS_R_System?$filter=ADSiteName eq 'TelAviv' and Client eq 1&$select=Name,ResourceId,ADSiteName")
    expected = [r for r in mock.CLASSES["SMS_R_System"]["rows"]() if r["ADSiteName"] == "TelAviv" and r["Client"] == 1]
    assert len(df) == len(expected) > 0
    assert set(df.columns) == {"Name", "ResourceId", "ADSiteName"}
    assert set(df["ADSiteName"]) == {"TelAviv"}


def test_results_drop_wmi_system_properties_and_flatten_lists(client):
    df = client.execute_query("SMS_R_System?$top=5")
    assert not [c for c in df.columns if c.startswith(("__", "@odata"))]
    assert df["IPAddresses"].map(lambda v: isinstance(v, str)).all()
    assert df["IPAddresses"].str.contains(",").any()


def test_lazy_properties_come_back_only_on_single_instance_reads(client):
    listed = client.execute_query("SMS_Collection")
    assert "CollectionRules" not in listed.columns
    one = client.execute_query("SMS_Collection('SMS00001')")
    assert len(one) == 1 and one.iloc[0]["CollectionID"] == "SMS00001"
    assert one.iloc[0]["CollectionRules"]


def test_wmi_class_names_are_case_insensitive_and_v1_names_are_corrected(client, server):
    lower = client.execute_query("sms_collection?$select=CollectionID")
    assert len(lower) == len(client.execute_query("SMS_Collection?$select=CollectionID"))
    host, pem = server
    v1 = MecmClient(server_url=host, username=ADMIN[0], password=ADMIN[1], ca_certificate=pem, include_v1=True)
    v1.get_schemas()
    assert len(v1.execute_query("v1.0/device?$top=3")) == 3


def test_v1_case_correction_also_works_from_an_indexed_catalog(server):
    host, pem = server
    fresh = MecmClient(server_url=host, username=ADMIN[0], password=ADMIN[1], ca_certificate=pem)
    fresh.attach_table_metadata([{"name": "v1.0/Device", "metadata_json": {}}])
    assert len(fresh.execute_query("v1.0/DEVICE?$top=2")) == 2


@pytest.mark.parametrize("query", [
    "SELECT * FROM SMS_R_System",
    "select Name from SMS_R_System where Client = 1",
    "https://elsewhere.example/AdminService/wmi/SMS_R_System",
    "wmi/$metadata", "wmi", "../../etc/passwd", "SMS_R_System;drop",
])
def test_invalid_targets_are_refused_before_any_request(client, mock, query):
    before = len(mock.REQUEST_LOG)
    with pytest.raises(ValueError):
        client.execute_query(query)
    assert len(mock.REQUEST_LOG) == before


def test_absolute_url_to_the_same_service_is_accepted(client):
    df = client.execute_query(f"{client.base_url}/wmi/SMS_Site")
    assert len(df) == 1


def test_server_odata_errors_are_surfaced_with_their_reason(client):
    with pytest.raises(RuntimeError) as err:
        client.execute_query("SMS_R_System?$filter=NoSuchProperty eq 1")
    assert "NoSuchProperty" in str(err.value) and "400" in str(err.value)
    with pytest.raises(RuntimeError, match="404"):
        client.execute_query("SMS_NoSuchClass")


def test_a_route_without_top_still_honors_max_rows(client, mock):
    mock.REJECT_TOP = True
    try:
        assert len(client.execute_query("SMS_R_System", max_rows=7)) == 7
    finally:
        mock.REJECT_TOP = False


@pytest.mark.parametrize("origin", ["https://attacker.example", "https://CM01.contoso.lab:443", "relative"])
def test_paging_stays_on_the_configured_host_whatever_the_next_link_says(client, mock, origin):
    # A nextLink is built by the server: it may name an alias (FQDN vs the
    # short name the admin typed), be relative, or be hostile. Every page must
    # still be read — and only ever from the configured AdminService.
    mock.NEXTLINK_ORIGIN = origin
    try:
        assert len(client.execute_query("SMS_G_System_INSTALLED_SOFTWARE")) == _total(mock, "SMS_G_System_INSTALLED_SOFTWARE")
    finally:
        mock.NEXTLINK_ORIGIN = None


def test_per_user_credentials_see_only_their_security_scope(server, mock):
    host, pem = server
    scoped = MecmClient(server_url=host, username="CONTOSO\\helpdesk_lon", password="Passw0rd!", ca_certificate=pem)
    names = set(scoped.execute_query("SMS_R_System?$select=Name,ADSiteName")["ADSiteName"])
    assert names == {"London"}


def test_the_client_only_ever_reads(client, mock):
    start = len(mock.REQUEST_LOG)
    client.test_connection()
    client.get_schemas(force_refresh=True)
    client.execute_query("SMS_R_System?$top=3")
    client.execute_query("SMS_Collection('SMS00001')")
    methods = {r["method"] for r in mock.REQUEST_LOG[start:]}
    assert methods == {"GET"}


def test_description_carries_the_odata_query_guide(client):
    text = client.description
    assert client.base_url in text
    assert "not a sql" in text.lower() and "$filter" in text and "SMS_FullCollectionMembership" in text


def test_registry_entry_resolves_with_windows_auth_variants():
    from app.schemas.data_source_registry import REGISTRY, resolve_client_class
    entry = REGISTRY["mecm"]
    assert resolve_client_class("mecm") is MecmClient
    assert set(entry.credentials_auth.by_auth) == {"ntlm", "kerberos"}
    assert "user" in entry.credentials_auth.by_auth["ntlm"].scopes
    assert entry.credentials_auth.by_auth["kerberos"].scopes == ["system"]
    # Every config/credential field the form renders is a constructor argument.
    import inspect
    params = set(inspect.signature(MecmClient.__init__).parameters)
    fields = set(entry.config_schema.model_fields)
    for variant in entry.credentials_auth.by_auth.values():
        fields |= set(variant.schema.model_fields)
    assert fields <= params


@pytest.mark.parametrize("fmt", ["full", "combined"])
def test_class_descriptions_reach_the_planner_schema_after_persistence(client, fmt):
    # The catalog keeps columns/keys/metadata_json, not Table.description, so the
    # description has to survive in metadata_json and be rendered from there —
    # it is what tells the planner what a cryptic WMI class holds.
    from app.ai.context.sections.tables_schema_section import TablesSchemaContext
    from app.ai.prompt_formatters import Table
    from app.schemas.data_source_schema import DataSourceSummarySchema

    persisted = [Table(name=t.name, columns=t.columns, pks=t.pks, fks=t.fks, metadata_json=t.metadata_json)
                 for t in client.get_schemas()]
    ctx = TablesSchemaContext(data_sources=[TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="1", name="SCCM", type="mecm"), tables=persisted)])
    out = ctx.render("full") if fmt == "full" else ctx.render_combined(top_k_per_ds=50, index_limit=200)
    from xml.sax.saxutils import escape
    for original in client.get_schemas():
        assert escape(original.description, {'"': "&quot;"}) in out or original.description in out, original.name


def test_metadata_with_entity_declarations_is_refused():
    # $metadata comes from a remote server; an entity-expansion payload must not be parsed.
    bomb = ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
            '<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx"><x>&b;</x></edmx:Edmx>')
    with pytest.raises(Exception) as err:
        parse_metadata(bomb)
    assert "entit" in type(err.value).__name__.lower() + str(err.value).lower()

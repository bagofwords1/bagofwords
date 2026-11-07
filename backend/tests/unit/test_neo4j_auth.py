"""Neo4j connector auth and admin settings: what the driver signs in with for
each auth option, impersonation on every session, Kerberos tokens (BOW's own
identity and per-user delegation), the per-connection row / sample limits, and
the read-only Graph Data Science allow-list.

Boundaries replaced: the Neo4j driver (the fake server from
test_neo4j_client), python-gssapi and the Kerberos ticket manager. The client,
the registry and the Cypher guard run for real.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.data_sources import kerberos as kerberos_mod
from app.data_sources.clients.cypher_guard import CypherNotAllowed, check_cypher
from app.data_sources.clients.neo4j_client import Neo4jClient
from app.schemas.data_source_registry import (
    REGISTRY,
    credentials_schema_for,
    member_bindable_fields,
)
from app.schemas.data_sources.configs import Neo4jBearerCredentials, Neo4jConfig
from app.services.connection_service import default_user_auth_modes
from tests.unit.test_neo4j_client import FakeNeo4j

URI = "bolt://graph.corp.example.com:7687"


@pytest.fixture
def server(monkeypatch):
    def make(answers=None, fail=None):
        fake = FakeNeo4j(answers, fail)
        monkeypatch.setattr("neo4j.GraphDatabase.driver", fake.driver)
        return fake
    return make


def _query(client):
    return client.execute_query("MATCH (n:Person) RETURN n.name AS name")


# ── the auth options offered ────────────────────────────────────────────────


def test_the_form_offers_each_auth_option_in_the_right_scope():
    by_auth = REGISTRY["neo4j"].credentials_auth.by_auth
    scopes = {name: set(v.scopes) for name, v in by_auth.items()}
    # Members may bring their own login or SSO token.
    assert {"system", "user"} <= scopes["userpass"]
    assert {"system", "user"} <= scopes["bearer"]
    # A secret that can act as other users, BOW's own Kerberos identity, and
    # no-auth are the admin's to set, never a member's.
    for admin_only in ("impersonation", "kerberos", "none"):
        assert scopes[admin_only] == {"system"}, admin_only
    # Kerberos delegation is per-user only, as for SQL Server.
    assert scopes["kerberos_delegated"] == {"user"}


@pytest.mark.parametrize("auth_type", ["userpass", "bearer", "impersonation", "kerberos", "none"])
def test_each_system_auth_option_builds_a_client(server, auth_type):
    """A form submission of each variant constructs a client: no field the
    schema asks for is one the client cannot take."""
    server()
    schema = credentials_schema_for("neo4j", auth_type)
    sample = {"username": "svc", "password": "pw", "token": "eyJ.x.y", "impersonated_user": "ann"}
    creds = schema(**{k: v for k, v in sample.items() if k in schema.model_fields}).model_dump()
    config = Neo4jConfig(uri=URI).model_dump()
    client = Neo4jClient(**{**config, **creds})
    assert client.auth_method in ("basic", "bearer", "kerberos", "none")


def test_member_auth_follows_the_system_auth():
    # Kerberos on the connection → members sign in by delegation, like SQL Server.
    assert default_user_auth_modes("neo4j", {"auth_type": "kerberos"}, {}) == ["kerberos_delegated"]
    # Impersonation → each member runs as the Neo4j user an admin mapped to them.
    assert member_bindable_fields("neo4j", "impersonation") == ["impersonated_user"]
    assert default_user_auth_modes("neo4j", {"auth_type": "impersonation"}, {}) == ["member_mapped"]
    # Password and token connections leave members to bring their own.
    for auth_type in ("userpass", "bearer"):
        assert default_user_auth_modes("neo4j", {"auth_type": auth_type}, {}) is None
        assert member_bindable_fields("neo4j", auth_type) == []


# ── what the driver signs in with ───────────────────────────────────────────


def test_password_auth_signs_in_with_the_saved_login(server):
    fake = server()
    _query(Neo4jClient(uri=URI, username="reader", password="pw"))
    assert fake.auths == [("reader", "pw")]


def test_a_bearer_token_signs_in_with_the_bearer_scheme(server):
    fake = server()
    token = Neo4jBearerCredentials(token="Bearer eyJhbGciOi.payload.sig").token
    _query(Neo4jClient(uri=URI, token=token))
    (auth,) = fake.auths
    assert auth.scheme == "bearer" and auth.credentials == "eyJhbGciOi.payload.sig"


def test_an_empty_token_is_refused_by_the_form():
    with pytest.raises(ValidationError):
        Neo4jBearerCredentials(token="Bearer   ")


def test_a_failed_connection_test_never_shows_the_token(server):
    token = "eyJ.secret-token-value.sig"
    server(fail=RuntimeError(f"auth failed for token {token}"))
    result = Neo4jClient(uri=URI, token=token).test_connection()
    assert result["success"] is False and token not in result["message"]


def test_no_auth_sends_no_credentials(server):
    fake = server()
    _query(Neo4jClient(uri=URI))
    assert fake.auths == [None]


# ── impersonation ───────────────────────────────────────────────────────────


def test_impersonation_runs_every_session_as_the_mapped_user(server):
    from neo4j.exceptions import ClientError

    fake = server({
        "dbms.components": (["name", "versions", "edition"], [["Neo4j Kernel", ["5.26"], "enterprise"]]),
        "db.schema.": ClientError("restricted"),
        "db.labels": (["label"], [["Person"]]),
        "db.relationshipTypes": (["relationshipType"], []),
        "UNWIND keys(n)": (["k"], [["name"]]),
    })
    client = Neo4jClient(uri=URI, username="svc", password="pw", impersonated_user="ann")
    _query(client)
    client.get_schemas()
    result = client.test_connection()
    assert fake.auths == [("svc", "pw")]  # signs in as the service user …
    assert fake.sessions and all(s["impersonated_user"] == "ann" for s in fake.sessions)  # … runs as ann
    assert result["success"] and "ann" in result["message"]


def test_without_impersonation_sessions_run_as_the_signed_in_user(server):
    fake = server()
    _query(Neo4jClient(uri=URI, username="svc", password="pw", impersonated_user="  "))
    assert all(s["impersonated_user"] is None for s in fake.sessions)


# ── Kerberos ────────────────────────────────────────────────────────────────


class FakeGssapi:
    """python-gssapi: initiating a context yields a token naming the target
    and the credential cache that was active."""

    class NameType:
        hostbased_service = "hostbased_service"
        kerberos_principal = "kerberos_principal"

    def __init__(self, manager):
        self.manager = manager
        self.contexts = []

    def Name(self, value, name_type):  # noqa: N802 - mirrors gssapi
        return SimpleNamespace(value=value, type=name_type)

    def Credentials(self, usage):  # noqa: N802
        return SimpleNamespace(usage=usage, ccache=self.manager.active, lifetime=3600)

    def SecurityContext(self, name, creds, usage):  # noqa: N802
        fake = self

        class Ctx:
            def step(self):
                fake.contexts.append((name.value, name.type, creds.ccache))
                return f"ap-req:{name.value}:{creds.ccache}:{len(fake.contexts)}".encode()

        return Ctx()


class FakeTicketManager:
    def __init__(self):
        self.active = "default"
        self.delegated = []

    def service_ccache(self, principal=None):
        return f"svc:{principal}" if principal else None

    def delegated_ccache(self, user_principal):
        self.delegated.append(user_principal)
        return f"user:{user_principal}"

    @contextmanager
    def activate(self, ccache):
        previous, self.active = self.active, ccache or "default"
        try:
            yield
        finally:
            self.active = previous


@pytest.fixture
def kerberos(monkeypatch):
    manager = FakeTicketManager()
    gssapi = FakeGssapi(manager)
    monkeypatch.setattr(kerberos_mod, "get_ticket_manager", lambda: manager)
    monkeypatch.setattr(kerberos_mod, "_import_gssapi", lambda: gssapi)
    return SimpleNamespace(manager=manager, gssapi=gssapi)


def _signed_in_token(fake):
    (manager,) = fake.auths
    auth = manager.get_auth()
    assert auth.scheme == "kerberos"
    return manager, base64.b64decode(auth.credentials).decode()


def test_kerberos_signs_in_with_a_token_for_the_neo4j_service_on_the_host(server, kerberos):
    fake = server()
    _query(Neo4jClient(uri=URI, use_kerberos=True))
    _, token = _signed_in_token(fake)
    assert token.startswith("ap-req:neo4j@graph.corp.example.com:default:")
    assert kerberos.gssapi.contexts[0][1] == "hostbased_service"


def test_kerberos_uses_the_configured_service_name_and_principal(server, kerberos):
    fake = server()
    _query(Neo4jClient(uri=URI, use_kerberos=True, kerberos_service="graphdb",
                       kerberos_principal="svc-bow@CORP.EXAMPLE.COM"))
    _, token = _signed_in_token(fake)
    assert token.startswith("ap-req:graphdb@graph.corp.example.com:svc:svc-bow@CORP.EXAMPLE.COM:")


def test_kerberos_delegation_signs_in_as_the_user(server, kerberos):
    fake = server()
    _query(Neo4jClient(uri=URI, use_kerberos=True, kerberos_impersonate="alice@CORP.EXAMPLE.COM",
                       kerberos_expected_sid="0105"))
    _, token = _signed_in_token(fake)
    assert ":user:alice@CORP.EXAMPLE.COM:" in token
    assert kerberos.manager.delegated == ["alice@CORP.EXAMPLE.COM"]


def test_kerberos_delegation_without_a_verified_directory_identity_is_refused(server, kerberos):
    fake = server()
    _query(Neo4jClient(uri=URI, use_kerberos=True, kerberos_impersonate="alice@CORP.EXAMPLE.COM"))
    (manager,) = fake.auths
    with pytest.raises(RuntimeError):
        manager.get_auth()
    assert kerberos.manager.delegated == []


def test_a_rejected_kerberos_token_is_replaced_once_the_server_refuses_it(server, kerberos):
    fake = server()
    _query(Neo4jClient(uri=URI, use_kerberos=True))
    manager, first = _signed_in_token(fake)
    assert manager.get_auth() is manager.get_auth()  # reused while valid
    rejected = manager.get_auth()
    retry = manager.handle_security_exception(rejected, SimpleNamespace(code="Neo.ClientError.Security.Unauthorized"))
    _, second = _signed_in_token(fake)
    assert retry is True and second != first
    # Errors that a new token cannot fix are not retried.
    assert manager.handle_security_exception(
        manager.get_auth(), SimpleNamespace(code="Neo.ClientError.Security.Forbidden")) is False


def test_kerberos_service_name_is_only_a_name():
    with pytest.raises(ValidationError):
        Neo4jConfig(uri=URI, kerberos_service="neo4j/host@REALM")
    assert Neo4jConfig(uri=URI).kerberos_service == "neo4j"


# ── admin settings ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("limit", [2, 7])
def test_the_row_limit_is_the_admins(server, limit):
    server({"RETURN x": (["x"], [[i] for i in range(20)])})
    client = Neo4jClient(uri=URI, max_rows=limit)
    df = client.execute_query("UNWIND range(1, 20) AS x RETURN x")
    assert len(df) == limit and f"{limit:,} rows" in df.attrs["bow_notice"]
    assert f"At most {limit:,} rows" in client.description


def test_settings_out_of_range_fall_back_inside_bounds():
    client = Neo4jClient(uri=URI, max_rows=10**9, schema_sample_size=0)
    assert client.max_rows == 100_000 and client.schema_sample_size == 1
    with pytest.raises(ValidationError):
        Neo4jConfig(uri=URI, max_rows=0)


def test_the_schema_sample_size_is_used_when_schema_procedures_are_restricted(server):
    from neo4j.exceptions import ClientError

    fake = server({
        "db.schema.nodeTypeProperties": ClientError("restricted"),
        "db.labels": (["label"], [["Person"]]),
        "db.relationshipTypes": (["relationshipType"], []),
        "UNWIND keys(n)": (["k"], [["name"]]),
    })
    tables = Neo4jClient(uri=URI, schema_sample_size=37).get_schemas()
    assert [t.name for t in tables] == ["(:Person)"]
    assert any("LIMIT 37" in cypher for cypher, _ in fake.statements)


# ── graph algorithms ────────────────────────────────────────────────────────

GDS_READS = [
    "CALL gds.graph.project('g1', 'Person', 'KNOWS')",
    "CALL gds.pageRank.stream('g1') YIELD nodeId, score RETURN gds.util.asNode(nodeId).name AS name, score",
    "CALL gds.louvain.stats('g1') YIELD communityCount RETURN communityCount",
    "CALL gds.nodeSimilarity.stream('g1') YIELD node1, node2, similarity RETURN node1, node2, similarity",
    "CALL gds.pageRank.stream.estimate('g1', {}) YIELD requiredMemory RETURN requiredMemory",
    "CALL gds.graph.drop('g1')",
    "CALL gds.list() YIELD name RETURN name",
    "MATCH (a:Person)-[:KNOWS]->(b:Person) WITH gds.graph.project('g2', a, b) AS g RETURN g.graphName",
]

GDS_REFUSED = [
    "CALL gds.pageRank.write('g1', {writeProperty: 'pr'})",
    "CALL gds.louvain.mutate('g1', {mutateProperty: 'c'})",
    "CALL gds.pageRank.write.estimate('g1', {writeProperty: 'pr'})",
    "CALL gds.graph.project.cypher('g', 'LOAD CSV FROM \"http://x\" AS r RETURN r', 'RETURN 1')",
    "CALL gds.graph.export('g1', {dbName: 'copy'})",
    "CALL gds.graph.nodeProperties.write('g1', ['pr'])",
    "CALL apoc.cypher.run('MATCH (n) RETURN n', {})",
    "RETURN apoc.text.join(['a'], ',')",
]


@pytest.mark.parametrize("query", GDS_READS)
def test_gds_reads_are_refused_unless_the_admin_allows_them(query):
    with pytest.raises(CypherNotAllowed):
        check_cypher(query)
    check_cypher(query, allow_gds=True)


@pytest.mark.parametrize("query", GDS_REFUSED)
def test_gds_write_modes_and_other_plugins_stay_refused_when_gds_is_allowed(query):
    with pytest.raises(CypherNotAllowed):
        check_cypher(query, allow_gds=True)


def test_the_client_applies_the_gds_setting_and_tells_the_agent(server):
    server()
    query = "CALL gds.pageRank.stream('g1') YIELD nodeId, score RETURN nodeId, score"
    with pytest.raises(CypherNotAllowed):
        Neo4jClient(uri=URI).execute_query(query)
    allowed = Neo4jClient(uri=URI, allow_gds=True)
    allowed.execute_query(query)
    assert "gds.pageRank.stream" in allowed.description
    assert "gds." not in Neo4jClient(uri=URI).description


def test_the_code_executor_guard_follows_the_connection_gds_setting():
    """Agent code passes through the executor's read-only guard before the
    client: it must apply the same per-connection GDS setting."""
    from app.ai.code_execution.sql_guard import ReadOnlyViolation, ensure_read_only

    query = "CALL gds.graph.drop('g1')"
    with pytest.raises(ReadOnlyViolation):
        ensure_read_only(Neo4jClient(uri=URI), query)
    ensure_read_only(Neo4jClient(uri=URI, allow_gds=True), query)
    with pytest.raises(ReadOnlyViolation):
        ensure_read_only(Neo4jClient(uri=URI, allow_gds=True), "CALL gds.pageRank.write('g1', {writeProperty: 'p'})")

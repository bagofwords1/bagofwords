"""Neo4jClient against a real Neo4j, the LTS line (5.26) and the newest
calendar-versioned release (what Aura runs). Needs Docker.

Covers what the unit tests can't: real record shapes, the schema procedures,
the server itself refusing a write in the READ session (with our Cypher guard
switched off), the server-side transaction timeout, the row cap, and the
direct connection on a neo4j:// URI.

Run:
    cd backend
    uv run pytest tests/integrations/test_neo4j_live.py -v
"""
from __future__ import annotations

import json

import pytest

testcontainers_neo4j = pytest.importorskip("testcontainers.neo4j")

from app.ai.code_execution.code_execution import QueryCapturingClientWrapper  # noqa: E402
from app.data_sources.clients import neo4j_client  # noqa: E402
from app.data_sources.clients.cypher_guard import CypherNotAllowed  # noqa: E402
from app.data_sources.clients.neo4j_client import Neo4jClient  # noqa: E402

IMAGES = ["neo4j:5.26-community", "neo4j:2026.10-community"]
PASSWORD = "test-password-123"

SEED = """
CREATE (keanu:Person {name: 'Keanu Reeves', born: 1964})
CREATE (carrie:Person {name: 'Carrie-Anne Moss', born: 1967})
CREATE (lana:Person {name: 'Lana Wachowski', born: 1965})
CREATE (matrix:Movie {title: 'The Matrix', released: 1999, premiere: date('1999-03-31')})
CREATE (reloaded:Movie {title: 'The Matrix Reloaded', released: 2003})
CREATE (keanu)-[:ACTED_IN {roles: ['Neo']}]->(matrix)
CREATE (carrie)-[:ACTED_IN {roles: ['Trinity']}]->(matrix)
CREATE (keanu)-[:ACTED_IN {roles: ['Neo']}]->(reloaded)
CREATE (lana)-[:DIRECTED]->(matrix)
CREATE (lana)-[:DIRECTED]->(reloaded)
"""


@pytest.fixture(scope="module", params=IMAGES)
def server(request):
    try:
        container = testcontainers_neo4j.Neo4jContainer(request.param, password=PASSWORD)
        container.start()
    except Exception as e:  # no Docker here
        pytest.skip(f"Docker / Neo4j container unavailable: {e}")
    with container.get_driver() as driver, driver.session() as s:
        s.run(SEED).consume()
    yield container
    container.stop()


def _client(server, scheme="bolt", **kw):
    host = server.get_container_host_ip()
    port = server.get_exposed_port(7687)
    return Neo4jClient(uri=f"{scheme}://{host}:{port}", username="neo4j", password=PASSWORD, **kw)


def test_connection_reports_the_server(server):
    res = _client(server).test_connection()
    assert res["success"], res["message"]
    assert "Neo4j" in res["message"]


def test_the_catalog_has_labels_relationships_and_patterns(server):
    tables = {t.name: t for t in _client(server).get_schemas()}
    assert {"(:Person)", "(:Movie)", "[:ACTED_IN]", "[:DIRECTED]"} <= set(tables)
    assert {c.name for c in tables["(:Movie)"].columns} >= {"title", "released"}
    assert "(:Person)-[:ACTED_IN]->(:Movie)" in tables["[:ACTED_IN]"].description


def test_queries_return_plain_tables(server):
    client = _client(server)
    df = client.execute_query(
        "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) WHERE m.released >= $since "
        "RETURN p.name AS actor, count(m) AS movies ORDER BY movies DESC, actor", params={"since": 1999})
    assert df.to_dict("records") == [{"actor": "Keanu Reeves", "movies": 2}, {"actor": "Carrie-Anne Moss", "movies": 1}]
    whole = client.execute_query("MATCH (m:Movie {title: 'The Matrix'}) RETURN m")
    assert whole.iloc[0]["m"] == {"_labels": ["Movie"], "title": "The Matrix", "released": 1999, "premiere": "1999-03-31"}
    path = client.execute_query("MATCH p = (:Person {name: 'Lana Wachowski'})-[:DIRECTED]->(:Movie {released: 1999}) RETURN p")
    assert [r["_type"] for r in path.iloc[0]["p"]["relationships"]] == ["DIRECTED"]


def test_params_go_through_the_executor_to_the_server(server):
    captured: list = []
    wrapper = QueryCapturingClientWrapper(_client(server), captured, [], client_key="main", query_timeout_seconds=30)
    df = wrapper.execute_query("MATCH (:Person)-[:ACTED_IN]->(m:Movie) WHERE m.title IN $titles RETURN DISTINCT m.title AS t",
                               params={"titles": ["The Matrix"]})
    assert list(df["t"]) == ["The Matrix"]
    assert json.loads(str(captured[0]))["params"] == {"titles": ["The Matrix"]}


def test_the_server_refuses_a_write_even_without_our_guard(server, monkeypatch):
    from neo4j.exceptions import ClientError

    monkeypatch.setattr(neo4j_client, "check_cypher", lambda q, **kw: None)
    with pytest.raises(ClientError):
        _client(server).execute_query("CREATE (:Intruder {at: datetime()})")
    with server.get_driver() as driver, driver.session() as s:
        assert s.run("MATCH (n:Intruder) RETURN count(n) AS c").single()["c"] == 0


def test_the_guard_stops_what_a_read_session_allows(server):
    with pytest.raises(CypherNotAllowed):
        _client(server).execute_query("LOAD CSV FROM 'http://169.254.169.254/latest/meta-data/' AS r RETURN r")


def test_a_long_query_is_stopped_by_the_server(server):
    from neo4j.exceptions import Neo4jError

    client = _client(server)
    client._query_budget = 1.0
    with pytest.raises(Neo4jError):
        client.execute_query("UNWIND range(1, 2000000000) AS x RETURN sum(x) AS s")


def test_the_row_cap_is_enforced_while_reading(server):
    df = _client(server, max_rows=3).execute_query("UNWIND range(1, 1000) AS x RETURN x")
    assert len(df) == 3 and "first 3 rows" in df.attrs["bow_notice"]


def test_a_routing_uri_connects_directly_to_the_server(server):
    """A neo4j:// URI is read as bolt://: the driver never follows a routing
    table to hosts no egress check saw. Against a single server, the
    advertised (container-internal) address would be unreachable from here,
    so a working connection proves the routing table was not used."""
    res = _client(server, scheme="neo4j").test_connection()
    assert res["success"], res["message"]
    df = _client(server, scheme="neo4j").execute_query("MATCH (m:Movie) RETURN count(m) AS c")
    assert df.iloc[0]["c"] == 2  # the seeded movies


# ── Enterprise: impersonation ───────────────────────────────────────────────
# Impersonation needs Neo4j Enterprise (evaluation license in the container).

ENTERPRISE_IMAGE = "neo4j:5.26-enterprise"


@pytest.fixture(scope="module")
def enterprise():
    try:
        container = testcontainers_neo4j.Neo4jContainer(ENTERPRISE_IMAGE, password=PASSWORD) \
            .with_env("NEO4J_ACCEPT_LICENSE_AGREEMENT", "eval")
        container.start()
    except Exception as e:  # no Docker here
        pytest.skip(f"Docker / Neo4j Enterprise container unavailable: {e}")
    with container.get_driver() as driver:
        driver.execute_query(SEED + " SET keanu.salary = 1000, carrie.salary = 2000")
        for q in (
            "CREATE USER svc SET PASSWORD 'svc-password-1' CHANGE NOT REQUIRED",
            "CREATE USER ann SET PASSWORD 'ann-password-1' CHANGE NOT REQUIRED",
            "CREATE USER bob SET PASSWORD 'bob-password-1' CHANGE NOT REQUIRED",
            "CREATE ROLE bow_service", "GRANT ROLE reader TO svc", "GRANT ROLE bow_service TO svc",
            "GRANT IMPERSONATE (ann) ON DBMS TO bow_service",
            "CREATE ROLE analyst AS COPY OF reader",
            "DENY READ {salary} ON GRAPH neo4j NODES Person TO analyst",
            "GRANT ROLE analyst TO ann", "GRANT ROLE reader TO bob",
        ):
            driver.execute_query(q, database_="system")
    yield container
    container.stop()


def _service_client(server, **kw):
    host, port = server.get_container_host_ip(), server.get_exposed_port(7687)
    return Neo4jClient(uri=f"bolt://{host}:{port}", username="svc", password="svc-password-1", **kw)


def test_impersonation_reads_with_the_impersonated_users_privileges(enterprise):
    q = "MATCH (p:Person) WHERE p.salary IS NOT NULL RETURN count(p) AS c"
    assert _service_client(enterprise).execute_query(q).iloc[0]["c"] == 2  # svc: a plain reader
    as_ann = _service_client(enterprise, impersonated_user="ann")
    assert as_ann.execute_query(q).iloc[0]["c"] == 0  # ann's role cannot read salary
    person = next(t for t in as_ann.get_schemas() if t.name == "(:Person)")
    assert "salary" not in {c.name for c in person.columns}
    assert as_ann.test_connection()["success"]


def test_impersonating_a_user_not_granted_is_refused_by_the_server(enterprise):
    result = _service_client(enterprise, impersonated_user="bob").test_connection()
    assert result["success"] is False

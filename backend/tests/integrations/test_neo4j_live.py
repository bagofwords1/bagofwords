"""Neo4jClient against a real Neo4j, the LTS line (5.26) and the newest
calendar-versioned release (what Aura runs). Needs Docker.

Covers what the unit tests can't: real record shapes, the schema procedures,
the server itself refusing a write in the READ session (with our Cypher guard
switched off), the server-side transaction timeout, the row cap, and the
routing-table check on a neo4j:// URI.

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


def _client(server, scheme="bolt"):
    host = server.get_container_host_ip()
    port = server.get_exposed_port(7687)
    return Neo4jClient(uri=f"{scheme}://{host}:{port}", username="neo4j", password=PASSWORD)


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

    monkeypatch.setattr(neo4j_client, "check_cypher", lambda q: None)
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


def test_the_row_cap_is_enforced_while_reading(server, monkeypatch):
    monkeypatch.setattr(neo4j_client, "MAX_ROWS", 3)
    df = _client(server).execute_query("UNWIND range(1, 1000) AS x RETURN x")
    assert len(df) == 3 and "first 3 rows" in df.attrs["bow_notice"]


def test_a_routing_uri_checks_every_address_in_the_routing_table(server, monkeypatch):
    seen = []
    import app.core.egress as egress

    monkeypatch.setattr(egress, "check_host", lambda host, *a, **k: seen.append(host) or [])
    client = _client(server, scheme="neo4j")
    try:
        client.test_connection()  # the advertised address may not be reachable from the host
    finally:
        assert seen, "the routing table's addresses were not checked"

    def refuse(host, *a, **k):
        raise egress.UnsafeURLError(f"Refusing {host}")

    monkeypatch.setattr(egress, "check_host", refuse)
    res = _client(server, scheme="neo4j").test_connection()
    assert res["success"] is False and "Refusing" in res["message"]

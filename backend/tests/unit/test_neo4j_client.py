"""Neo4j connector: read-only Cypher, plain values, the catalog, and how the
code executor passes Cypher parameters.

The driver is replaced only at its boundary (``Neo4jClient._read``, which
runs one read transaction). The real thing is covered by the integration
suite against a Neo4j container (tests/integrations/ds_clients.py).
"""
from __future__ import annotations

import json

import pytest

from app.ai.code_execution.code_execution import QueryCapturingClientWrapper, UnsafeSQLError
from app.ai.code_execution.sql_guard import ReadOnlyViolation, ensure_read_only
from app.data_sources.clients.cypher_guard import CypherNotAllowed, check_cypher
from app.data_sources.clients.neo4j_client import MAX_ROWS, Neo4jClient, to_plain, validate_uri
from app.schemas.data_source_registry import catalog_nouns_for, catalog_nouns_override, data_shape_for
from app.schemas.data_sources.configs import Neo4jConfig

READS = [
    "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) WHERE m.title = $t RETURN p.name AS name LIMIT 10",
    "MATCH (n:Delete) RETURN n.set AS s, n.`create` AS c",  # labels and properties named like clauses
    "MATCH (n) WHERE n.name = 'CREATE (x)' RETURN count(n) // DELETE everything",
    "MATCH (n) /* SET n.x = 1 */ RETURN n.name",
    'MATCH (n) WHERE n.s = "it\\"s DELETE" RETURN n',  # escaped quote inside a string
    "MATCH (n) RETURN n {set: 1, .name}",  # map key
    "CALL db.labels() YIELD label RETURN label",
    "CALL db.schema.visualization()",
    "MATCH p = shortestPath((a:Person {name: $a})-[*..5]-(b:Person {name: $b})) RETURN length(p)",
    "RETURN date.truncate('month', date()) AS m, duration.between(date(), date()) AS d",
    "MATCH (n) CALL { WITH n MATCH (n)--(m) RETURN count(m) AS c } RETURN n.name, c",
    "MATCH (n) WHERE n.age IN $ages RETURN n.name",
]

REFUSED = [
    "CREATE (n:Person {name: 'x'})",
    "match (n) detach delete n",
    "MATCH (n) SET n.x = 1",
    "MATCH (n) REMOVE n:Person",
    "MERGE (n:A)",
    "MATCH (n) FOREACH (x IN [1] | SET n.y = x)",
    "INSERT (:Person {name: 'x'})",  # GQL, Cypher 25
    "LOAD CSV FROM 'http://169.254.169.254/latest' AS r RETURN r",  # the server reads URLs itself
    "USE system MATCH (n) RETURN n",
    "Use neo4j MATCH (n) RETURN n",
    "SHOW USERS",
    "ALTER USER neo4j SET PASSWORD 'x'",
    "GRANT ROLE admin TO bob",
    "STOP DATABASE neo4j",
    "TERMINATE TRANSACTIONS 'x'",
    "MATCH (n) CALL { WITH n RETURN n } IN TRANSACTIONS RETURN n",
    "CALL apoc.load.json('http://x')",
    "CALL `apoc`.`load`.json('http://x')",
    "CALL dbms.security.listUsers()",
    "RETURN apoc.cypher.runFirstColumnSingle('MATCH (n) DETACH DELETE n', {})",
    "RETURN `apoc`.text.join(['a'], ',')",
    "RETURN genai.vector.encode('x', 'OpenAI', {})",
    "MATCH (n) WHERE n.x = 'unterminated RETURN n",
    "MATCH (n) /* never closed RETURN n",
]


@pytest.mark.parametrize("query", READS)
def test_read_only_cypher_passes(query):
    check_cypher(query)


@pytest.mark.parametrize("query", REFUSED)
def test_writes_admin_and_outbound_calls_are_refused(query):
    with pytest.raises(CypherNotAllowed):
        check_cypher(query)


def test_the_executor_guard_refuses_writes_for_neo4j_only():
    client = Neo4jClient(uri="bolt://db.example.com:7687", username="u", password="p")
    with pytest.raises(ReadOnlyViolation):
        ensure_read_only(client, "MATCH (n) DETACH DELETE n")
    ensure_read_only(client, "MATCH (n) RETURN count(n)")


@pytest.mark.parametrize("uri", ["neo4j+s://abc.databases.neo4j.io", "bolt://db:7687", "neo4j://10.0.0.5:7687",
                                 "bolt+ssc://db", "NEO4J+S://abc.databases.neo4j.io"])
def test_valid_uris(uri):
    assert validate_uri(uri).split("://")[0] in ("neo4j+s", "bolt", "neo4j", "bolt+ssc")


@pytest.mark.parametrize("uri", ["http://db:7474", "db:7687", "neo4j://neo4j:secret@db:7687", "bolt://"])
def test_invalid_uris_are_refused(uri):
    with pytest.raises(ValueError):
        Neo4jConfig(uri=uri)


def test_temporal_spatial_and_graph_values_become_plain():
    from neo4j.graph import Graph, Node, Relationship
    from neo4j.spatial import CartesianPoint
    from neo4j.time import Date, DateTime

    graph = Graph()
    node = Node(graph, "4:x:1", 1, ["Person"], {"name": "Ada", "born": Date(1815, 12, 10)})
    assert to_plain(node) == {"_labels": ["Person"], "name": "Ada", "born": "1815-12-10"}
    rel_type = type("ACTED_IN", (Relationship,), {})
    rel = rel_type(graph, "5:x:2", 2, {"roles": ["Neo"]})
    assert to_plain(rel) == {"_type": "ACTED_IN", "roles": ["Neo"]}
    assert to_plain(DateTime(2026, 11, 3, 10, 0, 0)).startswith("2026-11-03T10:00:00")
    assert to_plain(CartesianPoint((1.0, 2.0))) == {"x": 1.0, "y": 2.0, "srid": 7203}
    assert to_plain([float("nan"), {"a": b"\x01"}]) == [None, {"a": "01"}]


class _FakeNeo4j(Neo4jClient):
    """The real client with its one boundary, the read transaction, replaced."""

    def __init__(self, answers=None):
        super().__init__(uri="bolt://db.example.com:7687", username="u", password="p")
        self.answers = answers or {}
        self.calls = []

    def _read(self, cypher, params=None, *, limit=None, timeout=None):
        self.calls.append((cypher, params))
        for marker, answer in self.answers.items():
            if marker in cypher:
                return answer
        return ["name"], [["Ada"]], False


def test_params_bind_natively_through_the_executor_and_are_captured():
    client = _FakeNeo4j()
    captured: list = []
    wrapper = QueryCapturingClientWrapper(client, captured, [], client_key="main", query_timeout_seconds=30)
    query = "MATCH (:Person)-[:ACTED_IN]->(m:Movie) WHERE m.title = $t RETURN m.title AS name"
    df = wrapper.execute_query(query, params={"t": "The Matrix"})
    assert list(df["name"]) == ["Ada"]
    # Not rendered as SQL: the client got the Cypher and the params as they were.
    assert client.calls == [(query, {"t": "The Matrix"})]
    # Captured with the values, so the identity-bind check can see them.
    assert json.loads(str(captured[0])) == {"query": query, "params": {"t": "The Matrix"}}


def test_the_executor_refuses_a_write_before_it_reaches_neo4j():
    client = _FakeNeo4j()
    wrapper = QueryCapturingClientWrapper(client, [], [], client_key="main", query_timeout_seconds=30)
    with pytest.raises(UnsafeSQLError):
        wrapper.execute_query("MATCH (n) DETACH DELETE n")
    assert client.calls == []


def test_a_capped_result_tells_the_agent():
    client = _FakeNeo4j({"RETURN n.name": (["name"], [["a"]] * 3, True)})
    df = client.execute_query("MATCH (n) RETURN n.name")
    assert len(df) == 3 and f"{MAX_ROWS:,}" in df.attrs["bow_notice"]
    assert "bow_notice" not in client.execute_query("MATCH (n) RETURN n.title").attrs


def test_execute_query_takes_only_cypher_and_a_params_dict():
    client = _FakeNeo4j()
    with pytest.raises(ValueError):
        client.execute_query("")
    with pytest.raises(ValueError):
        client.execute_query("MATCH (n) RETURN n", params=["x"])
    with pytest.raises(CypherNotAllowed):
        client.execute_query("CREATE (n)")


def test_the_catalog_lists_labels_and_relationship_types_with_their_patterns():
    client = _FakeNeo4j({
        "nodeTypeProperties": (["nodeLabels", "propertyName", "propertyTypes"], [
            [["Person"], "name", ["String"]], [["Person"], "born", ["Long"]], [["Movie"], "title", ["String"]],
            [["Movie"], None, None]], False),
        "relTypeProperties": (["relType", "propertyName", "propertyTypes"], [
            [":`ACTED_IN`", "roles", ["StringArray"]], [":`DIRECTED`", None, None]], False),
    })
    client._schema_patterns = lambda timeout: [(["Person"], "ACTED_IN", ["Movie"]), (["Person"], "DIRECTED", ["Movie"])]
    tables = {t.name: t for t in client.get_schemas()}
    assert set(tables) == {"(:Person)", "(:Movie)", "[:ACTED_IN]", "[:DIRECTED]"}
    assert {c.name: c.dtype for c in tables["(:Person)"].columns} == {"name": "string", "born": "integer"}
    assert tables["[:ACTED_IN]"].columns[0].dtype == "array"
    assert "(:Person)-[:ACTED_IN]->(:Movie)" in tables["[:ACTED_IN]"].description
    assert tables["[:DIRECTED]"].metadata_json["patterns"] == ["(:Person)-[:DIRECTED]->(:Movie)"]


def test_restricted_schema_procedures_fall_back_to_samples():
    from neo4j.exceptions import ClientError

    class Restricted(_FakeNeo4j):
        def _read(self, cypher, params=None, *, limit=None, timeout=None):
            if "db.schema." in cypher:
                raise ClientError("procedure not allowed")
            return super()._read(cypher, params, limit=limit, timeout=timeout)

    client = Restricted({
        "db.labels": (["label"], [["Person"]], False),
        "db.relationshipTypes": (["relationshipType"], [["KNOWS"]], False),
        "MATCH (n:`Person`)": (["k"], [["name"]], False),
        "[r:`KNOWS`]": (["k"], [["since"]], False),
    })
    tables = {t.name: [c.name for c in t.columns] for t in client.get_schemas()}
    assert tables == {"(:Person)": ["name"], "[:KNOWS]": ["since"]}


def test_the_description_is_static_and_names_the_host_only():
    client = Neo4jClient(uri="neo4j+s://abc.databases.neo4j.io", username="reader", password="s3cret")
    text = client.description
    assert "abc.databases.neo4j.io" in text and "s3cret" not in text and "reader" not in text
    assert "$" in text and f"{MAX_ROWS:,}" in text


def test_a_failed_connection_test_never_shows_the_password():
    class Failing(Neo4jClient):
        def _read(self, *a, **k):
            raise RuntimeError("auth failed for neo4j with password s3cret-pass")

    res = Failing(uri="bolt://db:7687", username="neo4j", password="s3cret-pass").test_connection()
    assert res["success"] is False and "s3cret-pass" not in res["message"]


def test_a_query_timeout_leaves_a_margin_for_the_server_to_stop_first():
    client = Neo4jClient(uri="bolt://db:7687")
    client.apply_query_timeout(60)
    assert 5 <= client._query_budget < 60


def test_registry_entry():
    assert data_shape_for("neo4j") == "objects"
    assert catalog_nouns_for("neo4j") == ("node or relationship type", "node and relationship types")
    assert catalog_nouns_override("neo4j") == ["node or relationship type", "node and relationship types"]
    assert catalog_nouns_override("mongodb") is None  # others keep their data_shape noun

"""Neo4j connector: read-only Cypher, plain values, the catalog, how the code
executor passes Cypher parameters, and the identity-filter check on them.

Only the driver is replaced (``neo4j.GraphDatabase.driver``): a fake server
that answers ``tx.run`` and records what it was sent. Everything above it
(connect, the READ session, the transaction timeout, the row cap, the
catalog) is the real client. The real server is covered by
tests/integrations/test_neo4j_live.py.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ai.code_execution.code_execution import QueryCapturingClientWrapper, UnsafeSQLError
from app.ai.code_execution.query_params import verify_identity_binds_in_queries
from app.ai.code_execution.sql_guard import ReadOnlyViolation, ensure_read_only
from app.data_sources.clients import neo4j_client
from app.data_sources.clients.cypher_guard import CypherNotAllowed, check_cypher
from app.data_sources.clients.neo4j_client import MAX_ROWS, Neo4jClient, to_plain, validate_uri
from app.schemas.data_source_registry import catalog_nouns_for, catalog_nouns_override, data_shape_for
from app.schemas.data_sources.configs import Neo4jConfig
from app.schemas.param_schema import ParamSpec

READS = [
    "MATCH (p:Person)-[:ACTED_IN]->(m:Movie) WHERE m.title = $t RETURN p.name AS name LIMIT 10",
    "MATCH (n:Delete) RETURN n.set AS s, n.`create` AS c",  # labels and properties named like clauses
    "MATCH (n) WHERE n.name = 'CREATE (x)' RETURN count(n) // DELETE everything",
    "MATCH (n) /* SET n.x = 1 */ RETURN n.name",
    'MATCH (n) WHERE n.s = "it\\"s DELETE" RETURN n',  # escaped quote inside a string
    "MATCH (n) RETURN n {set: 1, .name, d: date.truncate('month', date())}",  # map keys, an allowed function
    "MATCH (n:L {p: $p}) RETURN n.name",
    "// comment\r\nMATCH (n) RETURN n.name",
    "MATCH (n) WHERE n.transactions > 1 RETURN n.name",
    "CALL db.labels() YIELD label RETURN label",
    "CALL db.schema.visualization()",
    "MATCH p = shortestPath((a:Person {name: $a})-[*..5]-(b:Person {name: $b})) RETURN length(p)",
    "RETURN date.truncate('month', date()) AS m, duration.between(date(), date()) AS d",
    "MATCH (n) CALL { WITH n MATCH (n)--(m) RETURN count(m) AS c } RETURN n.name, c",
    "MATCH (n) WHERE n.age IN $ages RETURN n.name",
    "MATCH (e:Event) RETURN e.start AS start, e.stop AS stop",  # aliases named like clauses
    "MATCH (set:Event) RETURN set.name AS show",  # a variable named like a clause
    "RETURN `date.truncate`('month', date()) AS m",
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
    "CALL { MATCH (n) RETURN n } IN CONCURRENT TRANSACTIONS RETURN n",
    "CALL { MATCH (n) RETURN n } IN 4 CONCURRENT TRANSACTIONS RETURN n",
    "CALL apoc.load.json('http://x')",
    "CALL `apoc`.`load`.json('http://x')",
    "CALL dbms.security.listUsers()",
    "RETURN apoc.cypher.runFirstColumnSingle('MATCH (n) DETACH DELETE n', {})",
    "RETURN `apoc`.text.join(['a'], ',')",
    "RETURN genai.vector.encode('x', 'OpenAI', {})",
    "RETURN myé.fn(1)",  # a namespace with a non-ASCII letter
    # Neo4j ends a // comment at \r too: what follows still runs.
    "// note\rCALL apoc.load.json('http://x')",
    "// note\rLOAD CSV FROM 'http://x' AS r RETURN r",
    "// note\rSHOW DATABASES",
    "// note\rUSE system MATCH (n) RETURN n",
    # A namespaced function in a map value is a call like any other.
    "RETURN {k: apoc.cypher.runFirstColumnSingle('MATCH (n) RETURN n', {})}",
    "MATCH (n) RETURN n {.name, k: genai.vector.encode('x', 'OpenAI', {})}",
    "MATCH (n:L {p: apoc.text.join(['a'], ',')}) RETURN n",
    "MATCH (n) WHERE n.x = 'unterminated RETURN n",
    "MATCH (n) /* never closed RETURN n",
    "RETURN `apoc.text.join`(['a'], ',')",  # one backticked name holding a namespace
    "MATCH (n) RETURN n // x\u2028LOAD CSV FROM 'http://x' AS r RETURN r",  # unusual line breaks are refused
    "MATCH (n) RETURN n // x\u0085USE system MATCH (m) RETURN m",
]


@pytest.mark.parametrize("query", READS)
def test_read_only_cypher_passes(query):
    check_cypher(query)


@pytest.mark.parametrize("query", REFUSED)
def test_writes_admin_and_outbound_calls_are_refused(query):
    with pytest.raises(CypherNotAllowed):
        check_cypher(query)


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


# ── a fake server at the driver boundary ─────────────────────────────────────

class _Record:
    def __init__(self, keys, row):
        self._keys, self._row = keys, row

    def values(self):
        return list(self._row)

    def __getitem__(self, key):
        return self._row[self._keys.index(key)]


class _Result:
    def __init__(self, keys, rows):
        self._keys, self._rows = keys, rows

    def keys(self):
        return list(self._keys)

    def __iter__(self):
        return (_Record(self._keys, r) for r in self._rows)


class FakeNeo4j:
    """Answers `tx.run` by the first marker found in the Cypher; records the
    URIs, sessions, statements and transaction timeouts it was given."""

    def __init__(self, answers=None, fail=None):
        self.answers = answers or {}
        self.fail = fail
        self.uris, self.sessions, self.statements, self.timeouts = [], [], [], []

    def driver(self, uri, auth=None, **kwargs):
        self.uris.append(uri)
        server = self

        class Session:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute_read(self, work):
                server.timeouts.append(getattr(work, "timeout", None))
                return work(SimpleNamespace(run=server.run))

        class Driver:
            def session(self, **kw):
                if server.fail:
                    raise server.fail
                server.sessions.append(kw)
                return Session()

            def close(self):
                pass

        return Driver()

    def run(self, cypher, params=None):
        self.statements.append((cypher, params))
        for marker, answer in self.answers.items():
            if marker in cypher:
                if isinstance(answer, Exception):
                    raise answer
                return _Result(*answer)
        return _Result(["name"], [["Ada"]])


@pytest.fixture
def server(monkeypatch):
    def make(answers=None, fail=None):
        fake = FakeNeo4j(answers, fail)
        monkeypatch.setattr("neo4j.GraphDatabase.driver", fake.driver)
        return fake
    return make


def _client(uri="bolt://db.example.com:7687"):
    return Neo4jClient(uri=uri, username="reader", password="s3cret-pass")


def test_every_query_runs_in_a_read_session(server):
    fake = server()
    _client().execute_query("MATCH (n) RETURN n.name AS name")
    assert fake.sessions and all(s["default_access_mode"] == "READ" for s in fake.sessions)


def test_a_routing_uri_connects_directly_to_the_host_the_admin_entered(server):
    """neo4j:// would let the server's routing table steer the driver to hosts
    no egress check saw. BOW always connects directly."""
    fake = server()
    _client("neo4j+s://abc.databases.neo4j.io").execute_query("MATCH (n) RETURN n.name AS name")
    _client("neo4j://db.internal:7687").execute_query("MATCH (n) RETURN n.name AS name")
    assert fake.uris == ["bolt+s://abc.databases.neo4j.io", "bolt://db.internal:7687"]


def test_one_driver_serves_every_query_of_a_client(server):
    """The driver pools connections; a new one per query would pay a full
    TCP + TLS + auth handshake each time."""
    fake = server()
    client = _client()
    for _ in range(3):
        client.execute_query("MATCH (n) RETURN n.name AS name")
    assert len(fake.uris) == 1 and len(fake.sessions) == 3


def test_the_executor_guard_refuses_writes_for_neo4j():
    with pytest.raises(ReadOnlyViolation):
        ensure_read_only(_client(), "MATCH (n) DETACH DELETE n")
    ensure_read_only(_client(), "MATCH (n) RETURN count(n)")


def test_params_reach_neo4j_unrendered_through_the_executor(server):
    fake = server()
    wrapper = QueryCapturingClientWrapper(_client(), [], [], client_key="main", query_timeout_seconds=30)
    query = "MATCH (:Person)-[:ACTED_IN]->(m:Movie) WHERE m.title = $t RETURN m.title AS name"
    df = wrapper.execute_query(query, params={"t": "The Matrix"})
    assert list(df["name"]) == ["Ada"]
    # Not rendered as SQL: `(:Person)` would have read as a `:Person` placeholder.
    assert fake.statements == [(query, {"t": "The Matrix"})]


def test_the_executor_refuses_a_write_before_it_reaches_neo4j(server):
    fake = server()
    wrapper = QueryCapturingClientWrapper(_client(), [], [], client_key="main", query_timeout_seconds=30)
    with pytest.raises(UnsafeSQLError):
        wrapper.execute_query("MATCH (n) DETACH DELETE n")
    assert fake.statements == []


def _identity_check(query, params, value):
    """Run one query through the real executor and the real identity check."""
    captured: list = []
    wrapper = QueryCapturingClientWrapper(_client(), captured, [], client_key="main", query_timeout_seconds=30)
    wrapper.execute_query(query, params=params)
    spec = ParamSpec(name="owner", type="string", source="identity")
    return verify_identity_binds_in_queries(captured, {"owner": value}, [spec])


@pytest.mark.parametrize("value", ["a@x.com", "ניר"])
@pytest.mark.parametrize("query", [
    "MATCH (o:Order) WHERE o.owner = $owner RETURN o.total AS t",
    "MATCH (o:Order {owner: $owner}) RETURN o.total AS t",
    "MATCH (o:Order)-[r:OWNS {by: $owner}]->(c) RETURN c.name AS t",
    "MATCH (o:Order) WHERE o.total > 0 AND (o.owner = $owner) RETURN o.total AS t",
    "MATCH (o:Order) WITH o WHERE o.owner IN $owner RETURN o.total AS t",
    "MATCH (o:Order) WHERE $owner = o.owner RETURN o.total AS t",
    "MATCH (o:Order {owner: $owner}) RETURN o.total AS t UNION MATCH (o:Order) WHERE o.owner = $owner RETURN o.total AS t",
])
def test_an_identity_value_filtering_the_query_passes_the_check(server, query, value):
    server()
    assert _identity_check(query, {"owner": value}, value) is None


@pytest.mark.parametrize("query", [
    "MATCH (o:Order) RETURN o.total AS t",  # passed but never used
    "MATCH (o:Order) WHERE o.total > 5 RETURN $owner AS who",  # used, but not as a filter
    "MATCH (o:Order) RETURN collect(o {k: $owner}) AS x",  # a map in a function call is not a pattern
    "MATCH (o:Order) WHERE o.owner = $owner OR 1 = 1 RETURN o.total AS t",  # OR: true without the value
    "MATCH (o:Order) WHERE NOT o.owner = $owner RETURN o.total AS t",
    "MATCH (o:Order) RETURN [x IN o.tags WHERE x = $owner] AS t",  # filters list elements, not rows
    "MATCH (o:Order) RETURN o.total AS t, [(o)-->(x {k: $owner}) | x] AS r",  # a pattern in a projection
    "MATCH (o:Order) OPTIONAL MATCH (o)-->(x) WHERE x.k = $owner RETURN o.total AS t",  # optional: keeps every o
    "MATCH (o:Order) WHERE true OR (o.owner = $owner) RETURN o.total AS t",  # OR around a parenthesised operand
    "MATCH (o:Order) WHERE NOT (o.owner = $owner) RETURN o.total AS t",
    "MATCH (o:Order {owner: $owner}) RETURN o.total AS t UNION MATCH (o:Order) RETURN o.total AS t",  # an unfiltered branch
    "MATCH (a:Order {owner: $owner}) MATCH (o:Order) RETURN o.total AS t",  # the filter is on another MATCH
    "MATCH (o:Order) WHERE o.owner <> $owner RETURN o.total AS t",  # everyone but the viewer
    "MATCH (o:Order) WHERE $owner = $owner RETURN o.total AS t",  # always true
])
def test_an_identity_value_that_does_not_filter_fails_the_check(server, query):
    server()
    assert _identity_check(query, {"owner": "a@x.com"}, "a@x.com") is not None


def test_a_capped_result_tells_the_agent(server, monkeypatch):
    monkeypatch.setattr(neo4j_client, "MAX_ROWS", 3)
    server({"RETURN x": (["x"], [[i] for i in range(10)])})
    df = _client().execute_query("UNWIND range(1, 10) AS x RETURN x")
    assert len(df) == 3 and "first 3 rows" in df.attrs["bow_notice"]
    assert "bow_notice" not in _client().execute_query("MATCH (n) RETURN n.name AS name").attrs


def test_the_transaction_timeout_stays_under_the_executor_budget(server):
    fake = server()
    client = _client()
    client.apply_query_timeout(60)
    client.execute_query("MATCH (n) RETURN n.name AS name")
    assert 5 <= fake.timeouts[-1] < 60


def test_execute_query_takes_only_cypher_and_a_params_dict(server):
    fake = server()
    with pytest.raises(ValueError):
        _client().execute_query("")
    with pytest.raises(ValueError):
        _client().execute_query("MATCH (n) RETURN n", params=["x"])
    with pytest.raises(CypherNotAllowed):
        _client().execute_query("CREATE (n)")
    assert fake.statements == []


def _viz():
    person = SimpleNamespace(element_id="n1", labels={"Person"})
    movie = SimpleNamespace(element_id="n2", labels={"Movie"})
    # Virtual relationships: their end nodes may come back without labels,
    # matched to the returned nodes by element id.
    def bare(n):
        return SimpleNamespace(element_id=n.element_id, labels=set())
    rels = [SimpleNamespace(type="ACTED_IN", start_node=bare(person), end_node=bare(movie)),
            SimpleNamespace(type="DIRECTED", start_node=bare(person), end_node=bare(movie))]
    return (["nodes", "relationships"], [[[person, movie], rels]])


def test_the_catalog_lists_labels_and_relationship_types_with_their_patterns(server):
    server({
        "nodeTypeProperties": (["nodeLabels", "propertyName", "propertyTypes"], [
            [["Person"], "name", ["String"]], [["Person"], "born", ["Long"]], [["Movie"], "title", ["String"]],
            [["Movie"], None, None]]),
        "relTypeProperties": (["relType", "propertyName", "propertyTypes"], [
            [":`ACTED_IN`", "roles", ["StringArray"]], [":`DIRECTED`", None, None]]),
        "visualization": _viz(),
    })
    tables = {t.name: t for t in _client().get_schemas()}
    assert set(tables) == {"(:Person)", "(:Movie)", "[:ACTED_IN]", "[:DIRECTED]"}
    assert {c.name: c.dtype for c in tables["(:Person)"].columns} == {"name": "string", "born": "integer"}
    assert tables["[:ACTED_IN]"].columns[0].dtype == "array"
    assert "(:Person)-[:ACTED_IN]->(:Movie)" in tables["[:ACTED_IN]"].description
    assert tables["[:DIRECTED]"].metadata_json["patterns"] == ["(:Person)-[:DIRECTED]->(:Movie)"]


def test_restricted_schema_procedures_fall_back_to_samples(server):
    from neo4j.exceptions import ClientError

    server({
        "db.schema.": ClientError("procedure not allowed"),
        "db.labels": (["label"], [["Person"]]),
        "db.relationshipTypes": (["relationshipType"], [["KNOWS"]]),
        "MATCH (n:`Person`)": (["k"], [["name"]]),
        "[r:`KNOWS`]": (["k"], [["since"]]),
    })
    tables = {t.name: [c.name for c in t.columns] for t in _client().get_schemas()}
    assert tables == {"(:Person)": ["name"], "[:KNOWS]": ["since"]}


def test_the_description_is_static_and_names_the_host_only():
    text = Neo4jClient(uri="neo4j+s://abc.databases.neo4j.io", username="reader", password="s3cret").description
    assert "abc.databases.neo4j.io" in text and "s3cret" not in text and "reader" not in text
    assert "$" in text and f"{MAX_ROWS:,}" in text


def test_the_connection_test_reports_the_server(server):
    server({"dbms.components": (["name", "versions", "edition"], [["Neo4j Kernel", ["5.26.32"], "community"]])})
    res = _client().test_connection()
    assert res["success"] is True and "5.26.32" in res["message"]


def test_a_failed_connection_test_never_shows_the_password(server):
    server(fail=RuntimeError("auth failed for reader with password s3cret-pass"))
    res = _client().test_connection()
    assert res["success"] is False and "s3cret-pass" not in res["message"]


def test_registry_entry():
    assert data_shape_for("neo4j") == "objects"
    assert catalog_nouns_for("neo4j") == ("node or relationship type", "node and relationship types")
    assert catalog_nouns_override("neo4j") == ["node or relationship type", "node and relationship types"]
    assert catalog_nouns_override("mongodb") is None  # no override: the UI keeps its data_shape noun

"""Read-only rules at the dispatch point of the non-SQL clients.

Each client is driven through its own ``execute_query`` with the transport
faked at the boundary (the pymongo driver, the Kusto client, the boto3 HTTP
send, the XMLA HTTP session). A refused call must raise ``ReadOnlyViolation``
and must not reach the fake; an allowed call must reach it unchanged.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.ai.code_execution.sql_guard import ReadOnlyViolation


def _assert_refused(call, sent):
    """Run `call`; nothing may have been sent, and the refusal must be a
    ReadOnlyViolation. The order matters: on code without the rule the
    first assertion shows what reached the transport."""
    error = None
    try:
        call()
    except Exception as exc:  # noqa: BLE001 - the type is asserted below
        error = exc
    assert sent == [], f"request sent: {sent!r}"
    assert isinstance(error, ReadOnlyViolation), f"not refused: {error!r}"


# ---------------------------------------------------------------------------
# MongoDB: aggregate stage allow-list, server-side JavaScript at any depth
# ---------------------------------------------------------------------------

class _FakeCursor(list):
    def max_time_ms(self, _ms):
        return self

    def sort(self, _spec):
        return self

    def limit(self, _n):
        return self


class _FakeCollection:
    def __init__(self, name, sent):
        self._name = name
        self._sent = sent

    def aggregate(self, pipeline, **kwargs):
        self._sent.append(("aggregate", self._name, pipeline))
        return iter([{"n": 1}])

    def find(self, filter_query, projection=None):
        self._sent.append(("find", self._name, filter_query, projection))
        return _FakeCursor([{"n": 1}])


class _FakeMongoClient:
    sent: list = []

    def __init__(self, uri, **kwargs):
        pass

    def __getitem__(self, _db_name):
        sent = self.sent

        class _Db:
            def __getitem__(self, coll_name):
                return _FakeCollection(coll_name, sent)

        return _Db()

    def close(self):
        pass


@pytest.fixture
def mongo(monkeypatch):
    from app.data_sources.clients import mongodb_client

    sent: list = []
    fake = type("_Mongo", (_FakeMongoClient,), {"sent": sent})
    monkeypatch.setattr(mongodb_client, "MongoClient", fake)
    client = mongodb_client.MongodbClient(host="db.example", database="app")
    return client, sent


def _mongo_query(**spec):
    return json.dumps({"collection": "orders", **spec})


_MONGO_REFUSED = {
    "out stage": [{"$match": {}}, {"$out": "copy"}],
    "merge stage": [{"$merge": {"into": "copy"}}],
    "union stage at the top": [{"$unionWith": {"coll": "other"}}],
    "stage name in another case": [{"$Out": "copy"}],
    "two operators in one stage": [{"$match": {}, "$out": "copy"}],
    "unknown stage": [{"$collStats": {}}],
    "stage that is not an object": ["$out"],
    "union stage inside a lookup pipeline": [
        {"$lookup": {"from": "other", "as": "o", "pipeline": [{"$unionWith": {"coll": "x"}}]}},
    ],
    "out stage inside a facet pipeline": [
        {"$facet": {"a": [{"$match": {}}], "b": [{"$out": "copy"}]}},
    ],
    "lookup pipeline that is not a list": [
        {"$lookup": {"from": "other", "as": "o", "pipeline": {"$out": "copy"}}},
    ],
    "facet branch that is not a list": [{"$facet": {"a": {"$out": "copy"}}}],
    "where in a match": [{"$match": {"$where": "true"}}],
    "function nested in a lookup pipeline": [
        {"$lookup": {"from": "other", "as": "o", "pipeline": [
            {"$match": {"$expr": {"$function": {"body": "f", "args": [], "lang": "js"}}}},
        ]}},
    ],
    "accumulator in a group": [
        {"$group": {"_id": None, "x": {"$accumulator": {"init": "f", "lang": "js"}}}},
    ],
    "where inside a facet branch": [
        {"$facet": {"a": [{"$match": {"$or": [{"$where": "true"}]}}]}},
    ],
}


@pytest.mark.parametrize("pipeline", list(_MONGO_REFUSED.values()), ids=list(_MONGO_REFUSED))
def test_mongo_refuses_pipeline_outside_the_read_stages(mongo, pipeline):
    client, sent = mongo
    _assert_refused(lambda: client.execute_query(_mongo_query(aggregate=pipeline)), sent)


def test_mongo_refuses_a_stage_name_written_with_a_json_escape(mongo):
    client, sent = mongo
    text = '{"collection": "orders", "aggregate": [{"\\u0024out": "copy"}]}'
    _assert_refused(lambda: client.execute_query(text), sent)


def test_mongo_refuses_an_aggregate_that_is_not_a_list(mongo):
    client, sent = mongo
    _assert_refused(lambda: client.execute_query(_mongo_query(aggregate={"$out": "copy"})), sent)


def test_mongo_refuses_javascript_in_a_find_filter(mongo):
    client, sent = mongo
    _assert_refused(
        lambda: client.execute_query(_mongo_query(find={"a": {"$elemMatch": {"$where": "true"}}})), sent)


def test_mongo_refuses_javascript_in_a_find_projection(mongo):
    client, sent = mongo
    query = _mongo_query(find={}, projection={"x": {"$function": {"body": "f", "args": [], "lang": "js"}}})
    _assert_refused(lambda: client.execute_query(query), sent)


@pytest.mark.parametrize("stage", ["$lookup", "$graphLookup"])
@pytest.mark.parametrize("source", [
    {"db": "local", "coll": "oplog.rs"},
    {"db": "config", "coll": "system.sessions"},
    ["users"],
    None,
], ids=["namespace object", "other database", "list", "missing"])
def test_mongo_refuses_a_lookup_source_that_is_not_a_collection_name(mongo, stage, source):
    client, sent = mongo
    spec = {"as": "o", "localField": "a", "foreignField": "b"}
    if stage == "$graphLookup":
        spec = {"as": "o", "startWith": "$a", "connectFromField": "a", "connectToField": "b"}
    if source is not None:
        spec["from"] = source
    _assert_refused(lambda: client.execute_query(_mongo_query(aggregate=[{stage: spec}])), sent)


def test_mongo_sends_a_lookup_from_a_collection_name(mongo):
    client, sent = mongo
    pipeline = [
        {"$lookup": {"from": "users", "localField": "a", "foreignField": "b", "as": "u"}},
        {"$graphLookup": {"from": "staff", "startWith": "$m", "connectFromField": "m",
                          "connectToField": "_id", "as": "chain"}},
    ]
    client.execute_query(_mongo_query(aggregate=pipeline))
    assert sent == [("aggregate", "orders", pipeline)]


def test_mongo_sends_a_read_pipeline_unchanged(mongo):
    client, sent = mongo
    pipeline = [
        {"$match": {"status": "active", "note": "$where"}},
        {"$lookup": {"from": "users", "as": "u", "pipeline": [{"$match": {}}, {"$project": {"name": 1}}]}},
        {"$facet": {"a": [{"$group": {"_id": "$k", "n": {"$sum": 1}}}], "b": [{"$count": "n"}]}},
        {"$unwind": "$u"},
        {"$sort": {"n": -1}},
        {"$limit": 5},
    ]
    df = client.execute_query(_mongo_query(aggregate=pipeline))
    assert isinstance(df, pd.DataFrame) and len(df) == 1
    assert sent == [("aggregate", "orders", pipeline)]


def test_mongo_sends_a_find_unchanged(mongo):
    client, sent = mongo
    client.execute_query(_mongo_query(find={"status": "active"}, limit=10))
    assert sent == [("find", "orders", {"status": "active"}, None)]


# ---------------------------------------------------------------------------
# Azure Data Explorer: management commands are refused, queries go to the
# query endpoint
# ---------------------------------------------------------------------------

class _FakeKustoClient:
    """Routes like azure.kusto.data.KustoClient.execute (a leading '.' goes
    to the management endpoint) and records every call."""

    sent: list = []

    def __init__(self, _kcsb):
        pass

    def _record(self, endpoint, query):
        self.sent.append((endpoint, query))
        return SimpleNamespace(primary_results=[])

    def execute(self, database, query, properties=None):
        return self._record("mgmt" if query.startswith(".") else "query", query)

    def execute_query(self, database, query, properties=None):
        return self._record("query", query)

    def execute_mgmt(self, database, query, properties=None):
        return self._record("mgmt", query)

    def close(self):
        pass


@pytest.fixture
def adx(monkeypatch):
    from app.data_sources.clients import azure_data_explorer_client as adx_module

    sent: list = []
    fake = type("_Kusto", (_FakeKustoClient,), {"sent": sent})
    monkeypatch.setattr(adx_module, "KustoClient", fake)
    client = adx_module.AzureDataExplorerClient(
        cluster_url="https://c.example.kusto.windows.net", database="db",
        client_id="id", client_secret="secret", tenant_id="tenant",
    )
    return client, sent


_BOM = chr(0xFEFF)
_ZWSP = chr(0x200B)
_LINE_SEP = chr(0x2028)

_KQL_REFUSED = {
    "leading dot": ".drop table T",
    "read command": ".show tables",
    "after whitespace": "  \t\n.drop table T",
    "after a comment": "// note\n.drop table T",
    "after comments and blank lines": "// a\r\n\n  // b\n  .set-or-append T <| print x=1",
    "after a byte order mark": _BOM + ".drop table T",
    "after a zero width space": _ZWSP + ".drop table T",
    "comment ended by a line separator": "// a" + _LINE_SEP + ".drop table T",
    "second statement": "T | take 1;\n.drop table T",
    "second statement after a comment": "T | take 1; // x\n  .drop table T",
    "second statement after a let": "let n = 1;.drop table T",
    "line that starts with a dot": "T | take 1\n.drop table T",
    "second statement after a string holding slashes": "T | where u == 'http://x' ; .drop table T",
}


@pytest.mark.parametrize("kql", list(_KQL_REFUSED.values()), ids=list(_KQL_REFUSED))
def test_adx_refuses_management_commands(adx, kql):
    client, sent = adx
    _assert_refused(lambda: client.execute_query(kql), sent)


@pytest.mark.parametrize("kql", [
    "T | take 10",
    "// note\nT | where Name == '.x' | take 1",
    "let n = 1;\nT | take n",
    "T | where Path endswith '.csv'; ",
])
def test_adx_sends_queries_to_the_query_endpoint(adx, kql):
    client, sent = adx
    client.execute_query(kql)
    assert sent == [("query", kql)]


# ---------------------------------------------------------------------------
# AWS Cost Explorer: only get_/list_/describe_ API operations
# ---------------------------------------------------------------------------

class _RawBody:
    def __init__(self, body: bytes):
        self._body = body

    def stream(self, **_kwargs):
        yield self._body


@pytest.fixture
def aws_cost(monkeypatch):
    from botocore.awsrequest import AWSResponse

    from app.data_sources.clients.aws_cost_client import AwsCostClient

    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    client = AwsCostClient(access_key="AKIDEXAMPLE", secret_key="example")
    sent: list = []

    def _send(request, **_kwargs):
        target = request.headers.get("X-Amz-Target")
        sent.append(target.decode() if isinstance(target, bytes) else target)
        body = b'{"ResultsByTime": []}'
        return AWSResponse(request.url, 200, {}, _RawBody(body))

    client.session.events.register("before-send", _send)
    return client, sent


_ARN = "arn:aws:ce::123456789012:anomalymonitor/x"


@pytest.mark.parametrize("operation, parameters", [
    ("delete_anomaly_monitor", {"MonitorArn": _ARN}),
    ("delete_cost_category_definition", {"CostCategoryArn": _ARN}),
    ("update_cost_allocation_tags_status",
     {"CostAllocationTagsStatus": [{"TagKey": "team", "Status": "Active"}]}),
    ("start_savings_plans_purchase_recommendation_generation", {}),
    ("get_paginator", {}),
    ("get_waiter", {}),
    ("GET_COST_AND_USAGE", {}),
    ("get_cost_and_usage ", {}),
    ("_make_api_call", {}),
    ("close", {}),
])
def test_aws_cost_refuses_operations_outside_reads(aws_cost, operation, parameters):
    client, sent = aws_cost
    _assert_refused(lambda: client.execute_query(operation, parameters), sent)


def test_aws_cost_refuses_a_non_string_operation(aws_cost):
    client, sent = aws_cost
    _assert_refused(lambda: client.execute_query(["get_cost_and_usage"], {}), sent)


def test_aws_cost_sends_a_read_operation(aws_cost):
    client, sent = aws_cost
    df = client.execute_query("get_cost_and_usage", {
        "TimePeriod": {"Start": "2024-12-01", "End": "2024-12-05"},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
    })
    assert isinstance(df, pd.DataFrame)
    assert sent == ["AWSInsightsIndexService.GetCostAndUsage"]


def test_aws_cost_sends_a_list_operation(aws_cost):
    client, sent = aws_cost
    # Sent; its answer has no ResultsByTime, which the client reports.
    with pytest.raises(RuntimeError, match="Unexpected response structure"):
        client.execute_query("list_cost_category_definitions", {})
    assert sent == ["AWSInsightsIndexService.ListCostCategoryDefinitions"]


# ---------------------------------------------------------------------------
# XMLA family: leading verb allow-list and the catalog check
# ---------------------------------------------------------------------------

_EXECUTE_OK = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
    '<ExecuteResponse xmlns="urn:schemas-microsoft-com:xml-analysis"><return>'
    '<root xmlns="urn:schemas-microsoft-com:xml-analysis:rowset">'
    "<row><A>1</A></row>"
    "</root></return></ExecuteResponse></soap:Body></soap:Envelope>"
).encode()


def _xmla_session(client):
    """Install a recording HTTP session; returns the list of sent statements
    paired with the catalog each request carried."""
    from defusedxml import ElementTree as ET

    sent: list = []

    def _post(url, data=None, headers=None, timeout=None, verify=None):
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            # Still a request on the wire: record the raw body.
            sent.append(("unparsed body", data))
            raise
        texts = {el.tag.split("}", 1)[-1]: el.text for el in root.iter()}
        sent.append((texts.get("Statement"), texts.get("Catalog")))
        resp = MagicMock()
        resp.status_code = 200
        resp.content = _EXECUTE_OK
        resp.text = _EXECUTE_OK.decode()
        return resp

    session = MagicMock()
    session.post.side_effect = _post
    client._http = session
    return sent


def _analysis_services(**kwargs):
    from app.data_sources.clients.analysis_services_client import AnalysisServicesClient

    client = AnalysisServicesClient(host="https://ssas.example/olap/msmdpump.dll",
                                    username="u", password="p", **kwargs)
    # The platform attaches the indexed tables, so a table name resolves
    # without a live metadata crawl.
    client.attach_table_metadata([{"name": "AW/Sales", "metadata_json": {
        "analysis_services": {"catalog": "AW", "supportsDax": True}}}])
    return client


def _infor_olap(**kwargs):
    from app.data_sources.clients.infor_olap_client import InforOlapClient

    return InforOlapClient(host="http://epm.example/bi/olap", username="u", password="p", **kwargs)


_XMLA_FACTORIES = {"analysis_services": _analysis_services, "infor_olap": _infor_olap}

_XMLA_REFUSED = {
    "drop": "DROP MEMBER [Sales].[Measures].[X]",
    "writeback": "UPDATE CUBE [Sales] SET ([Measures].[X]) = 1",
    "create": "CREATE MEMBER [Sales].[Measures].[X] AS 1",
    "lower case write": "update cube [Sales] set ([Measures].[X]) = 1",
    "json command": '{"refresh": {"type": "full", "objects": [{"database": "AW"}]}}',
    "insert": "INSERT INTO [Model] ([A]) OPENQUERY([DS], 'SELECT 1')",
    "verb after a line comment": "-- note\nDELETE FROM [Model]",
    "verb after a slash comment": "// note\nDROP MINING STRUCTURE [S]",
    "verb after a block comment": "/* SELECT */ DROP MINING STRUCTURE [S]",
    "comment that hides a nested block": "/* a /* b */ SELECT 1 */ DROP MINING STRUCTURE [S]",
    "unterminated block comment": "/* SELECT 1",
    "line comment ended by a line separator": "-- SELECT" + _LINE_SEP + "DROP MINING STRUCTURE [S]",
    "select into a new model": "SELECT INTO [New] USING [Microsoft_Decision_Trees] FROM [Old]",
    "select into after a comment": "SELECT /* x */ INTO [New] FROM [Old]",
    "openquery in a read": "SELECT FROM [M] PREDICTION JOIN OPENQUERY([DS], 'q') AS t ON 1 = 1",
    "openrowset in a read": "SELECT * FROM OPENROWSET('p', 'c', 'q')",
    "second statement": "SELECT {} ON 0 FROM [Sales]; DROP MEMBER [Sales].[Measures].[X]",
    "second statement after a comment": "EVALUATE T; -- x\n DELETE FROM [Model]",
    "verb in a lookalike letter": chr(0x17F) + "ELECT {} ON 0 FROM [Sales]",
    "unknown verb": "DRILLTHROUGH SELECT {} ON 0 FROM [Sales]",
    "unterminated string": "EVALUATE ROW(\"a\", \"b; DROP",
    "unterminated bracket": "SELECT {[Measures].[A} ON 0 FROM [Sales]; DROP",
}


@pytest.mark.parametrize("factory", list(_XMLA_FACTORIES.values()), ids=list(_XMLA_FACTORIES))
@pytest.mark.parametrize("statement", list(_XMLA_REFUSED.values()), ids=list(_XMLA_REFUSED))
def test_xmla_refuses_statements_outside_the_read_verbs(factory, statement):
    client = factory(catalog="AW")
    sent = _xmla_session(client)
    _assert_refused(lambda: client.execute_query(statement), sent)


# Characters some readers take as a line end and others do not. A comment
# ended by one of them reads differently to the check and to a server that
# ends comments only at a line feed, so a statement holding one is refused.
_XMLA_SEPARATORS = {
    "vertical tab": chr(0x0B),
    "form feed": chr(0x0C),
    "next line": chr(0x85),
    "line separator": "\N{LINE SEPARATOR}",
    "paragraph separator": "\N{PARAGRAPH SEPARATOR}",
}


def _separator_shapes():
    for name, sep in _XMLA_SEPARATORS.items():
        # The check would read SELECT first; such a server reads DROP first.
        yield f"{name}: hidden first verb", (
            "-- x" + sep + "SELECT {} ON 0 FROM [S]\nDROP MINING STRUCTURE [S]")
        # The check would read a literal from the quote to the last quote.
        yield f"{name}: quote hides a second statement", (
            "SELECT {} ON 0 FROM [S] --" + sep + "'\n; DROP MINING STRUCTURE [S] --'")
        yield f"{name}: quote hides into", (
            "SELECT --" + sep + "'\nINTO [New] USING [Microsoft_Decision_Trees] FROM [Old] --'")
        yield f"{name}: in a literal", "EVALUATE ROW(\"a\", \"x" + sep + "y\")"


_XMLA_SEPARATOR_SHAPES = dict(_separator_shapes())


@pytest.mark.parametrize("factory", list(_XMLA_FACTORIES.values()), ids=list(_XMLA_FACTORIES))
@pytest.mark.parametrize("statement", list(_XMLA_SEPARATOR_SHAPES.values()), ids=list(_XMLA_SEPARATOR_SHAPES))
def test_xmla_refuses_a_statement_holding_an_ambiguous_line_end(factory, statement):
    client = factory(catalog="AW")
    sent = _xmla_session(client)
    _assert_refused(lambda: client.execute_query(statement), sent)


# Statements with CR line ends: XML reads each CR LF and lone CR as LF, so
# the statement the server parses is the one on the right.
_XMLA_ALLOWED_CR = {
    "-- note\r\nSELECT {[Measures].[Sales]} ON COLUMNS\r\nFROM [Sales]\r\n":
        "-- note\nSELECT {[Measures].[Sales]} ON COLUMNS\nFROM [Sales]\n",
    "// note\r\nEVALUATE\r\n  TOPN(5, 'Sales') // tail\r\n":
        "// note\nEVALUATE\n  TOPN(5, 'Sales') // tail\n",
    "SELECT {} ON 0\tFROM [Sales] -- note\r":
        "SELECT {} ON 0\tFROM [Sales] -- note\n",
}


@pytest.mark.parametrize("factory", list(_XMLA_FACTORIES.values()), ids=list(_XMLA_FACTORIES))
@pytest.mark.parametrize("statement", list(_XMLA_ALLOWED_CR))
def test_xmla_sends_statements_with_carriage_returns(factory, statement):
    client = factory(catalog="AW")
    sent = _xmla_session(client)
    client.execute_query(statement)
    assert sent == [(_XMLA_ALLOWED_CR[statement], "AW")]


_XMLA_ALLOWED = [
    "-- a\n// b\nDEFINE MEASURE Sales[N] = 1\nEVALUATE ROW(\"n\", [N])\n",
    "SELECT {[Measures].[Sales]} ON COLUMNS FROM [Sales]",
    "select {} on 0 from [Sales];",
    "  -- note\n/* c */ // d\nWITH MEMBER [Measures].[X] AS 1 SELECT {[Measures].[X]} ON 0 FROM [Sales]",
    "EVALUATE TOPN(5, 'Sales')",
    "DEFINE MEASURE Sales[N] = COUNTROWS(Sales) EVALUATE ROW(\"n\", [N])",
    "EVALUATE FILTER('Into', 'Into'[A] = \"; DROP\")",
    "SELECT [Into] FROM $SYSTEM.TMSCHEMA_TABLES",
    "SELECT {[Measures].[A]]]} ON 0 FROM [Sales] -- trailing; DROP",
]


@pytest.mark.parametrize("factory", list(_XMLA_FACTORIES.values()), ids=list(_XMLA_FACTORIES))
@pytest.mark.parametrize("statement", _XMLA_ALLOWED)
def test_xmla_sends_read_statements_unchanged(factory, statement):
    client = factory(catalog="AW")
    sent = _xmla_session(client)
    client.execute_query(statement)
    assert sent == [(statement, "AW")]


@pytest.mark.parametrize("factory", list(_XMLA_FACTORIES.values()), ids=list(_XMLA_FACTORIES))
def test_xmla_refuses_a_catalog_other_than_the_configured_one(factory):
    client = factory(catalog="AW")
    sent = _xmla_session(client)
    _assert_refused(lambda: client.execute_query("SELECT {} ON 0 FROM [Sales]", catalog="Finance"), sent)


@pytest.mark.parametrize("factory", list(_XMLA_FACTORIES.values()), ids=list(_XMLA_FACTORIES))
def test_xmla_refuses_a_catalog_the_table_name_does_not_name(factory):
    client = factory()
    sent = _xmla_session(client)
    _assert_refused(
        lambda: client.execute_query("SELECT {} ON 0 FROM [Sales]", "AW/Sales", catalog="Finance"), sent)


@pytest.mark.parametrize("factory", list(_XMLA_FACTORIES.values()), ids=list(_XMLA_FACTORIES))
def test_xmla_refuses_a_catalog_override_with_nothing_to_match(factory):
    client = factory()
    sent = _xmla_session(client)
    _assert_refused(lambda: client.execute_query("SELECT {} ON 0 FROM [Sales]", catalog="Finance"), sent)


@pytest.mark.parametrize("factory", list(_XMLA_FACTORIES.values()), ids=list(_XMLA_FACTORIES))
def test_xmla_accepts_the_configured_catalog_in_any_case(factory):
    client = factory(catalog="AW")
    sent = _xmla_session(client)
    client.execute_query("SELECT {} ON 0 FROM [Sales]", catalog="aw")
    assert sent == [("SELECT {} ON 0 FROM [Sales]", "AW")]


@pytest.mark.parametrize("factory", list(_XMLA_FACTORIES.values()), ids=list(_XMLA_FACTORIES))
def test_xmla_accepts_the_catalog_the_table_name_names(factory):
    client = factory()
    sent = _xmla_session(client)
    client.execute_query("SELECT {} ON 0 FROM [Sales]", "AW/Sales", catalog="AW")
    assert sent == [("SELECT {} ON 0 FROM [Sales]", "AW")]


def _json_response(body, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    resp.text = json.dumps(body)
    return resp


# ---------------------------------------------------------------------------
# Infor EPM: the MDX sent through the BOW_ExecuteMdx process
# ---------------------------------------------------------------------------

@pytest.fixture
def infor_epm():
    from app.data_sources.clients.infor_epm_client import InforEpmClient

    client = InforEpmClient(api_url="https://ion.example/epm", olap_database="DEMO_OLAP",
                            bearer_token="token", async_mode=False)
    sent: list = []

    def _post(url, json=None, **kwargs):
        sent.append((url.rsplit("/", 1)[-1], json))
        return _json_response({"result": "[Region].[North]\tNUM\t1\n"})

    session = MagicMock()
    session.post.side_effect = _post
    client._http = session
    return client, sent


_INFOR_EPM_REFUSED = {
    "drop": "DROP MEMBER [Sales].[SalesMeasures].[X]",
    "writeback": "UPDATE CUBE [Sales] SET ([SalesMeasures].[X]) = 1",
    "second statement": "SELECT {} ON 0 FROM [Sales]; DROP MEMBER [Sales].[SalesMeasures].[X]",
    "select into": "SELECT INTO [New] FROM [Sales]",
    "verb after a comment": "-- note\nDELETE FROM [Sales]",
    "unknown verb": "DRILLTHROUGH SELECT {} ON 0 FROM [Sales]",
    # The process may pass a lone CR on as written, where a server ending
    # comments only at LF would read the quoted text below as code.
    "lone carriage return in a comment": (
        "SELECT {} ON 0 FROM [Sales] --\r'\n; DROP MEMBER [Sales].[SalesMeasures].[X] --'"),
    "lone carriage return": "SELECT {} ON 0\rFROM [Sales]",
    "ambiguous line end": "-- x" + _LINE_SEP + "SELECT {} ON 0 FROM [S]\nDROP MEMBER [S].[M].[X]",
}


@pytest.mark.parametrize("statement", list(_INFOR_EPM_REFUSED.values()), ids=list(_INFOR_EPM_REFUSED))
def test_infor_epm_refuses_statements_outside_one_read(infor_epm, statement):
    client, sent = infor_epm
    _assert_refused(lambda: client.execute_query(statement, "DEMO_OLAP/Sales"), sent)


@pytest.mark.parametrize("statement", [
    "SELECT {[SalesMeasures].[Revenue]} ON COLUMNS, [Region].Members ON ROWS FROM [Sales]",
    "WITH MEMBER [SalesMeasures].[X] AS 1 SELECT {[SalesMeasures].[X]} ON 0 FROM [Sales]",
    "-- note\r\nSELECT {} ON 0\r\nFROM [Sales] // tail\r\n",
    "SELECT {[Region].[Into]} ON 0 FROM [Sales] WHERE ([Period].[2024]);",
])
def test_infor_epm_sends_a_read_unchanged(infor_epm, statement):
    client, sent = infor_epm
    client.execute_query(statement, "DEMO_OLAP/Sales")
    assert [(process, body["mdxQuery"], body["OLAPName"]) for process, body in sent] == [
        ("BOW_ExecuteMdx", statement.strip(), "DEMO_OLAP")]


# ---------------------------------------------------------------------------
# Oracle BI: Logical SQL to XmlViewService.executeXMLQuery
# ---------------------------------------------------------------------------

_ORACLE_BI_EMPTY_ROWSET = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
    'xmlns:sawsoap="urn://oracle.bi.webservices/v12"><soap:Body>'
    "<sawsoap:executeXMLQueryResult><sawsoap:return><sawsoap:rowset></sawsoap:rowset>"
    "</sawsoap:return></sawsoap:executeXMLQueryResult></soap:Body></soap:Envelope>"
).encode()


@pytest.fixture
def oracle_bi():
    import re
    from xml.sax.saxutils import unescape

    from app.data_sources.clients.oracle_bi_client import OracleBIClient

    client = OracleBIClient(host="https://oac.example", username="u", password="p")
    sent: list = []

    def _post(url, data=None, **kwargs):
        logical = re.search(r"<logicalSQL>(.*?)</logicalSQL>", data.decode(), re.S)
        sent.append(unescape(logical.group(1)) if logical else ("other request", url))
        resp = MagicMock()
        resp.status_code = 200
        resp.content = _ORACLE_BI_EMPTY_ROWSET
        resp.text = _ORACLE_BI_EMPTY_ROWSET.decode()
        return resp

    session = MagicMock()
    session.post.side_effect = _post
    client._http = session
    client._session_id = "session"
    return client, sent


_ORACLE_BI_REFUSED = {
    "execute physical": 'EXECUTE PHYSICAL CONNECTION POOL "DB"."Pool" DELETE FROM t',
    "execute physical in lower case and spread over lines": (
        'execute\n  physical connection pool "DB"."Pool" delete from t'),
    "execute physical after a select": (
        'SELECT "P"."P" FROM "A"; EXECUTE PHYSICAL CONNECTION POOL "DB"."Pool" DELETE FROM t'),
    "execute physical after a comment and a separator": (
        'SELECT "P"."P" FROM "A" /* x */ ; // y\nEXECUTE PHYSICAL CONNECTION POOL "DB"."Pool" DELETE FROM t'),
    "execute inside a select": 'SELECT "P"."P" FROM "A" WHERE 1 = (EXECUTE PHYSICAL CONNECTION POOL p x)',
    "set variable prefix": 'SET VARIABLE LOGLEVEL = 3; SELECT "P"."P" FROM "A"',
    "colon separated second statement": 'SELECT "P"."P" FROM "A": SELECT "P"."P" FROM "A"',
    "procedure call": "CALL SAPurgeAllCache()",
    "physical select": "SELECT_PHYSICAL a FROM db.schema.t",
    "physical expression": """SELECT EVALUATE('f(%1)', "P"."P") FROM "A\"""",
    "physical aggregate": """SELECT EVALUATE_AGGR('f(%1)', "P"."P") FROM "A\"""",
    "physical predicate": """SELECT "P"."P" FROM "A" WHERE EVALUATE_PREDICATE('f(%1)', "P"."P")""",
    "physical analytic": """SELECT EVALUATE_ANALYTIC('f(%1)', "P"."P") FROM "A\"""",
    "double dash, which the reference does not list as a comment": (
        'SELECT "P"."P" FROM "A" -- \'\n; EXECUTE PHYSICAL CONNECTION POOL p x --\''),
    "backslash in a string": """SELECT "P"."P" FROM "A" WHERE "P"."P" = 'a\\' OR 1 = 1 --'""",
    "backslash in a quoted name": 'SELECT "P\\" FROM "A"',
    "unterminated string": """SELECT "P"."P" FROM "A" WHERE "P"."P" = 'a; EXECUTE PHYSICAL x""",
    "unterminated quoted name": 'SELECT "P"."P FROM A',
    "unterminated block comment": 'SELECT "P"."P" FROM "A" /* x',
    "nested block comment": 'SELECT "P"."P" FROM "A" /* a /* b */ EXECUTE PHYSICAL x */',
    "line comment ended by a line separator": (
        'SELECT "P"."P" FROM "A" // x' + _LINE_SEP + 'EXECUTE PHYSICAL CONNECTION POOL p x'),
    "line comment ended by a vertical tab": '# x' + chr(0x0B) + 'EXECUTE PHYSICAL CONNECTION POOL p x',
    "verb in a lookalike letter": chr(0x17F) + 'ELECT "P"."P" FROM "A"',
    "insert": 'INSERT INTO "A" VALUES (1)',
    "no statement": "/* only a comment */",
    "not text": {"logicalSQL": 'SELECT "P"."P" FROM "A"'},
}


@pytest.mark.parametrize("statement", list(_ORACLE_BI_REFUSED.values()), ids=list(_ORACLE_BI_REFUSED))
def test_oracle_bi_refuses_statements_outside_one_select(oracle_bi, statement):
    client, sent = oracle_bi
    _assert_refused(lambda: client.execute_query(statement), sent)


@pytest.mark.parametrize("statement", [
    'SELECT "Products"."Product", "Base Facts"."Revenue" FROM "A - Sample Sales"',
    'select "P"."P" from "A" where "P"."P" = \'it\'\'s; EXECUTE PHYSICAL\' order by 1;',
    'SELECT "Execute"."Physical", "Evaluate"."X" FROM "A"',
    '/* lead */ // note\n# hash note\r\nSELECT "P"."P" FROM "A" // EXECUTE PHYSICAL in a comment',
    'WITH s AS (SELECT "P"."P" FROM "A") SELECT * FROM s',
    '(SELECT "P"."P" FROM "A") UNION ALL (SELECT "P"."Q" FROM "A")',
    'SELECT "P"."P" FROM "A" WHERE "P"."T" = TIMESTAMP \'2024-01-01 10:00:00\'',
    'SELECT "P"."P" FROM "A" WHERE "P"."P" IN (SELECT "P"."P" FROM "A" EXCEPT SELECT "P"."Q" FROM "A")',
])
def test_oracle_bi_sends_a_select_unchanged(oracle_bi, statement):
    client, sent = oracle_bi
    client.execute_query(statement)
    assert sent == [statement]


# ---------------------------------------------------------------------------
# OpenSearch: the SQL plugin key and the envelope keys
# ---------------------------------------------------------------------------

@pytest.fixture
def opensearch(monkeypatch):
    from app.data_sources.clients.opensearch_client import OpenSearchClient

    sent: list = []

    def _request(method, url, json=None, **kwargs):
        sent.append((method, url.split("://", 1)[1].split("/", 1)[1], json))
        if url.endswith("/_plugins/_sql"):
            return _json_response({"schema": [{"name": "n"}], "datarows": [[1]]})
        return _json_response({"hits": {"hits": []}})

    monkeypatch.setattr("app.data_sources.clients.opensearch_client.pinned_request", _request)
    return OpenSearchClient(host="http://search.example:9200"), sent


_OPENSEARCH_REFUSED = {
    "delete": {"sql": "DELETE FROM orders WHERE status = 'x'"},
    "second statement": {"sql": "SELECT 1 FROM orders; DELETE FROM orders"},
    "ddl": {"sql": "CREATE TABLE copy (a INT)"},
    "drop": {"sql": "DROP TABLE orders"},
    "select into": {"sql": "SELECT * INTO copy FROM orders"},
    "update": {"sql": "UPDATE orders SET status = 'x'"},
    "not a select": {"sql": "SHOW TABLES LIKE orders"},
    "unparsable": {"sql": "SELECT FROM WHERE"},
    "line comment": {"sql": "SELECT status FROM orders -- note"},
    "hash comment": {"sql": "SELECT status FROM orders # note"},
    "block comment": {"sql": "SELECT /* note */ status FROM orders"},
    "backslash": {"sql": "SELECT status FROM orders WHERE status = 'a\\''"},
    "not text": {"sql": ["SELECT 1"]},
    "empty": {"sql": "  "},
    "unknown key beside the search": {"index": "orders", "query": {"match_all": {}}, "script": {"source": "x"}},
    "unknown key beside the sql": {"sql": "SELECT 1 FROM orders", "pipeline": "p"},
}


@pytest.mark.parametrize("envelope", list(_OPENSEARCH_REFUSED.values()), ids=list(_OPENSEARCH_REFUSED))
def test_opensearch_refuses_sql_outside_one_select_and_unknown_envelope_keys(opensearch, envelope):
    client, sent = opensearch
    _assert_refused(lambda: client.execute_query(json.dumps(envelope)), sent)


@pytest.mark.parametrize("sql", [
    "SELECT status, COUNT(*) AS n FROM orders GROUP BY status",
    "select `order id`, total from orders where status = 'it''s' order by total desc limit 5",
    "WITH big AS (SELECT * FROM orders WHERE total > 10) SELECT COUNT(*) FROM big",
    "SELECT status FROM orders UNION SELECT status FROM returns",
    "SELECT status FROM orders WHERE note = 'DELETE; -- #'",
])
def test_opensearch_sends_a_select_unchanged(opensearch, sql):
    client, sent = opensearch
    client.execute_query(json.dumps({"sql": sql}))
    assert sent == [("POST", "_plugins/_sql", {"query": sql})]


def test_opensearch_sends_a_search_unchanged(opensearch):
    client, sent = opensearch
    client.execute_query(json.dumps({"index": "orders", "query": {"match_all": {}}, "size": 5}))
    assert sent == [("POST", "orders/_search", {"query": {"match_all": {}}, "size": 5, "timeout": "60s"})]


# ---------------------------------------------------------------------------
# Sisense: the /sql endpoint of a data model
# ---------------------------------------------------------------------------

@pytest.fixture
def sisense():
    from app.data_sources.clients.sisense_client import SisenseClient

    client = SisenseClient(host="https://sisense.example", api_token="token")
    sent: list = []

    def _get(url, params=None, **kwargs):
        sent.append((url.split("/api/", 1)[1], params["query"]))
        return _json_response({"headers": ["n"], "values": [[1]]})

    session = MagicMock()
    session.get.side_effect = _get
    client._http = session
    client._access_token = "token"
    return client, sent


_SISENSE_REFUSED = {
    "delete": "DELETE FROM Customers",
    "insert": "INSERT INTO Customers (ID) VALUES (1)",
    "update": "UPDATE Customers SET Status = 'x'",
    "ddl": "CREATE TABLE Copy (ID INT)",
    "drop": "DROP TABLE Customers",
    "second statement": "SELECT * FROM Customers; DROP TABLE Customers",
    "select into": "SELECT * INTO Copy FROM Customers",
    "not a select": "PRAGMA table_info(Customers)",
    "unparsable": "SELECT FROM WHERE",
    "line comment": "SELECT * FROM Customers -- note",
    "block comment": "SELECT * FROM Customers /* note */",
    "hash": "SELECT * FROM Customers # note",
    "backslash": "SELECT * FROM Customers WHERE Status = 'a\\''",
    "not text": ["SELECT * FROM Customers"],
}


@pytest.mark.parametrize("sql", list(_SISENSE_REFUSED.values()), ids=list(_SISENSE_REFUSED))
def test_sisense_refuses_sql_outside_one_select(sisense, sql):
    client, sent = sisense
    _assert_refused(lambda: client.execute_query(sql, datasource_title="Sales Model"), sent)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM Customers",
    'SELECT "first name", "last name" FROM "Order Details"',
    "SELECT [Order Details].[Amount] FROM [Order Details] WHERE Status = 'Active' LIMIT 10",
    "SELECT Category, SUM(Amount) AS Total FROM Orders GROUP BY Category ORDER BY Total DESC LIMIT 10",
    "WITH a AS (SELECT ID FROM Customers) SELECT COUNT(*) FROM a",
    "SELECT ID FROM Customers UNION SELECT ID FROM Orders",
])
def test_sisense_sends_a_select_unchanged(sisense, sql):
    client, sent = sisense
    client.execute_query(sql, datasource_title="Sales Model")
    assert sent == [("datasources/Sales%20Model/sql", sql)]


# ---------------------------------------------------------------------------
# Timbr A2A: every prompt asks Timbr to validate the SQL it generates
# ---------------------------------------------------------------------------

def _timbr_a2a(**kwargs):
    from app.data_sources.clients.timbr_a2a_client import TimbrA2aClient

    client = TimbrA2aClient(host="https://timbr.example", ontology="sales", api_key="key", **kwargs)
    sent: list = []

    def _post(url, json=None, headers=None, **kw):
        sent.append((url.rsplit("/", 2)[-2:], json, dict(headers or {})))
        return _json_response({"status": "success", "data": {"rows": [{"n": 1}]}})

    session = MagicMock()
    session.post.side_effect = _post
    client._session = session
    return client, sent


@pytest.mark.parametrize("setup", [
    lambda: _timbr_a2a(should_validate_sql=False),
    lambda: _timbr_a2a(should_validate_sql="no"),
    lambda: _timbr_a2a(should_validate_sql=None),
], ids=["turned off", "not a boolean", "unset"])
def test_timbr_a2a_refuses_a_prompt_without_sql_validation(setup):
    client, sent = setup()
    _assert_refused(lambda: client.execute_query("How many orders shipped last week?"), sent)


def test_timbr_a2a_refuses_a_prompt_when_validation_is_turned_off_after_construction():
    client, sent = _timbr_a2a()
    client.should_validate_sql = False
    _assert_refused(lambda: client.execute_query("How many orders shipped last week?"), sent)


def test_timbr_a2a_sends_a_prompt_unchanged_with_sql_validation_on():
    client, sent = _timbr_a2a()
    df = client.execute_query("How many orders shipped last week?")
    assert df["n"].tolist() == [1]
    assert len(sent) == 1
    (path, body, headers) = sent[0]
    assert path == ["execute", "prompt"]
    assert body == {"prompt": "How many orders shipped last week?"}
    assert [v for k, v in headers.items() if k.lower() == "should-validate-sql"] == ["true"]

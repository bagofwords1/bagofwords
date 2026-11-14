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
        root = ET.fromstring(data)
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


_XMLA_ALLOWED = [
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

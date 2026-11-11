"""Unit tests for the DynamoDB, Cosmos DB, Couchbase and Cassandra clients.

Real-server behavior (catalog from a live cluster, key queries, aggregation,
caps) is exercised in tests/integrations/test_nosql_live.py. These tests pin
what needs no server: the read-only statement guard every dialect shares,
schema inference over sampled documents, value normalization, the DynamoDB
paging / cap contract against a stubbed botocore client, and registry wiring.
"""
from __future__ import annotations

import decimal

import boto3
import pytest
from botocore.stub import Stubber

from app.data_sources.clients._document_schema import (
    ReadOnlyQueryError,
    ensure_single_select,
    infer_columns,
    rows_to_frame,
    to_plain,
)

TYPES = {
    "dynamodb": "DynamodbClient",
    "cosmosdb": "CosmosdbClient",
    "couchbase": "CouchbaseClient",
    "cassandra": "CassandraClient",
}


# ------------------------------------------------------------------- guard


@pytest.mark.parametrize("stmt", [
    "SELECT * FROM t",
    "  select a, b from \"Orders\" where \"pk\" = 'x' ;  ",
    "SELECT * FROM t WHERE note = 'please delete; then update'",
    'SELECT "Update" FROM "Delete"',
    "(SELECT 1)",
    "SELECT * FROM t -- trailing ; DROP TABLE t",
])
def test_guard_allows_a_single_read(stmt):
    assert ensure_single_select(stmt, "X").upper().lstrip("( ").startswith("SELECT")


@pytest.mark.parametrize("stmt", [
    "DELETE FROM t",
    "UPDATE t SET a = 1",
    "INSERT INTO t VALUE {'a': 1}",
    "UPSERT INTO t (KEY, VALUE) VALUES ('k', {})",
    "DROP TABLE t",
    "SELECT 1; DELETE FROM t",
    "SELECT 1 ; SELECT 2",
    "BEGIN BATCH INSERT INTO t (a) VALUES (1) APPLY BATCH",
    "",
    "   ",
    "/* SELECT */ DELETE FROM t",
])
def test_guard_refuses_writes_and_multi_statements(stmt):
    with pytest.raises(ReadOnlyQueryError):
        ensure_single_select(stmt, "X")


def test_guard_extra_read_verbs_are_opt_in():
    with pytest.raises(ReadOnlyQueryError):
        ensure_single_select("INFER `b`", "SQL++")
    assert ensure_single_select("INFER `b`", "SQL++", extra_read_verbs=("INFER",))


def test_guard_error_is_a_value_error():
    # The sandbox surfaces ValueError text to the agent so it can fix the query.
    assert issubclass(ReadOnlyQueryError, ValueError)


# ------------------------------------------------------- schema inference


def test_infer_columns_unions_nested_paths_across_documents():
    docs = [
        {"id": "1", "total": 10, "address": {"city": "Paris"}, "items": [{"sku": "a", "qty": 1}]},
        {"id": "2", "total": 10.5, "address": {"zip": "75001"}, "tags": ["x"]},
        {"id": "3", "status": None},
    ]
    cols = {c.name: c.dtype for c in infer_columns(docs, key_order=["id"])}
    assert list(cols)[0] == "id"
    assert cols["total"] == "number"  # 10 and 10.5 are the same JSON type
    assert cols["address.city"] == "string" and cols["address.zip"] == "string"
    assert cols["items"] == "array" and cols["items[].qty"] == "integer"
    assert cols["tags"] == "array"
    assert cols["status"] == "null"


def test_infer_columns_reports_conflicting_types():
    cols = {c.name: c.dtype for c in infer_columns([{"v": "1"}, {"v": 1}, {"v": None}])}
    assert set(cols["v"].split("|")) == {"integer", "string"}


def test_infer_columns_is_bounded_for_deep_documents():
    doc: dict = {}
    cur = doc
    for i in range(12):
        cur[f"l{i}"] = {}
        cur = cur[f"l{i}"]
    cur["leaf"] = 1
    cols = infer_columns([doc])
    assert len(cols) == 1 and cols[0].dtype == "object"
    assert cols[0].name.count(".") < 12


def test_to_plain_makes_driver_values_json_friendly():
    import datetime as dt
    import json

    value = {
        "n": decimal.Decimal("3"), "f": decimal.Decimal("2.5"), "s": {"b", "a"},
        "raw": b"\x00\x01", "when": dt.datetime(2025, 1, 2, 3, 4, 5), "nested": [decimal.Decimal("1.0")],
    }
    plain = to_plain(value)
    json.dumps(plain)
    assert plain["n"] == 3 and isinstance(plain["n"], int)
    assert plain["f"] == 2.5
    assert plain["s"] == ["a", "b"]
    assert plain["when"].startswith("2025-01-02")


def test_rows_to_frame_puts_keys_first():
    df = rows_to_frame([{"b": 1, "pk": "x", "a": 2}], key_order=["pk"])
    assert list(df.columns)[0] == "pk"
    assert rows_to_frame([]).empty


# ---------------------------------------------------------------- registry


@pytest.mark.parametrize("type_name,cls_name", TYPES.items())
def test_registered_with_an_explicit_client_path(type_name, cls_name):
    from app.schemas.data_source_registry import REGISTRY, resolve_client_class

    entry = REGISTRY[type_name]
    assert entry.client_path.endswith(f".{cls_name}")
    assert resolve_client_class(type_name).__name__ == cls_name
    assert entry.data_shape == "tables"


@pytest.mark.parametrize("type_name", TYPES)
def test_every_auth_variant_is_accepted_by_the_client(type_name):
    from app.schemas.data_source_registry import REGISTRY, resolve_client_class

    entry = REGISTRY[type_name]
    cls = resolve_client_class(type_name)
    base = {f: (fi.default if fi.default is not None and not fi.is_required() else "x")
            for f, fi in entry.config_schema.model_fields.items()}
    for variant in entry.credentials_auth.by_auth.values():
        creds = dict.fromkeys(variant.schema.model_fields, "x")
        cls(**base, **creds)  # constructing never connects


@pytest.mark.parametrize("type_name", TYPES)
def test_description_tells_the_agent_it_is_read_only(type_name):
    from app.schemas.data_source_registry import REGISTRY, resolve_client_class

    entry = REGISTRY[type_name]
    base = {f: (fi.default if fi.default is not None and not fi.is_required() else "x")
            for f, fi in entry.config_schema.model_fields.items()}
    text = resolve_client_class(type_name)(**base).description
    assert "read-only" in text.lower()
    assert "execute_query" in text


# ---------------------------------------------------------------- DynamoDB


def _ddb_client(**kw):
    from app.data_sources.clients.dynamodb_client import DynamodbClient

    c = DynamodbClient(region="us-east-1", access_key="x", secret_key="y", **kw)
    raw = boto3.client("dynamodb", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
    c._ddb = raw
    return c, Stubber(raw)


def _item(i):
    return {"pk": {"S": f"c-{i}"}, "total": {"N": str(i * 1.5)}, "tags": {"SS": ["b", "a"]}}


def test_dynamodb_follows_pages_until_the_cap_and_flags_truncation():
    c, stub = _ddb_client(max_items=3)
    stub.add_response("execute_statement", {"Items": [_item(1), _item(2)], "NextToken": "t1"},
                      {"Statement": 'SELECT * FROM "T"', "Limit": 3})
    stub.add_response("execute_statement", {"Items": [_item(3)], "NextToken": "t2"},
                      {"Statement": 'SELECT * FROM "T"', "Limit": 1, "NextToken": "t1"})
    stub.add_client_error("describe_table", "ResourceNotFoundException")
    with stub:
        df = c.execute_query('SELECT * FROM "T"')
    assert len(df) == 3
    assert df.attrs["truncated"] is True
    assert df["tags"].iloc[0] == ["a", "b"]
    assert df["total"].iloc[1] == 3.0


def test_dynamodb_exhausted_result_is_not_truncated():
    c, stub = _ddb_client(max_items=10)
    stub.add_response("execute_statement", {"Items": [_item(1)]})
    stub.add_client_error("describe_table", "ResourceNotFoundException")
    with stub:
        df = c.execute_query('SELECT * FROM "T" WHERE "pk" = ?', parameters=["c-1"])
    assert len(df) == 1 and df.attrs["truncated"] is False


def test_dynamodb_refuses_writes_before_any_call():
    c, stub = _ddb_client()
    with stub:  # no responses queued: any AWS call would fail the test
        for bad in ['DELETE FROM "T" WHERE "pk" = \'1\'', 'UPDATE "T" SET a=1 WHERE pk=\'1\'',
                    'INSERT INTO "T" VALUE {\'pk\': \'1\'}']:
            with pytest.raises(ValueError):
                c.execute_query(bad)
        stub.assert_no_pending_responses()


def test_dynamodb_catalog_names_keys_and_indexes():
    c, stub = _ddb_client(tables="Orders")
    stub.add_response("list_tables", {"TableNames": ["Orders", "Other"]})
    stub.add_response("describe_table", {"Table": {
        "TableName": "Orders",
        "AttributeDefinitions": [{"AttributeName": "pk", "AttributeType": "S"},
                                 {"AttributeName": "sk", "AttributeType": "N"}],
        "KeySchema": [{"AttributeName": "sk", "KeyType": "RANGE"}, {"AttributeName": "pk", "KeyType": "HASH"}],
        "GlobalSecondaryIndexes": [{"IndexName": "by_sk", "KeySchema": [{"AttributeName": "sk", "KeyType": "HASH"}],
                                    "Projection": {"ProjectionType": "ALL"}}],
        "ItemCount": 0,
    }})
    stub.add_response("scan", {"Items": [{"pk": {"S": "a"}, "sk": {"N": "1"}, "city": {"S": "Paris"}}]})
    with stub:
        tables = c.get_schemas()
    assert [t.name for t in tables] == ["Orders"]  # allowlist applied
    t = tables[0]
    assert [col.name for col in t.columns][:2] == ["pk", "sk"]  # partition key first
    assert t.columns[1].dtype == "number"
    assert '"Orders"."by_sk"' in t.description
    assert "0 items" not in t.description  # a lagging ItemCount isn't reported as empty
    assert t.metadata_json["dynamodb"]["keys"] == ["pk", "sk"]

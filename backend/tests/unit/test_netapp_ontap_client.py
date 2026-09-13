"""ONTAP contracts exercised at the HTTP boundary; no appliance credentials."""

import io
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from app.data_sources.clients.netapp_ontap_client import NetAppOntapClient, OntapQueryError
from app.schemas.data_source_registry import resolve_client_class


def test_registry_exposes_diagnostic_table_contract():
    cls = resolve_client_class("netapp_ontap")
    client = cls(url="https://ontap.example", username="reader", password="test-only")
    tables = {t.name: t for t in client.get_tables()}
    assert {"volumes", "volume_metrics", "ems_events", "volume_aggregates"} <= tables.keys()
    assert any(t.startswith("diag_") for t in tables)
    assert tables["volume_metrics"].metadata_json["netapp"]["parents"]


@pytest.fixture
def client():
    c = NetAppOntapClient(url="https://ontap.example", username="reader", password="test-only")
    yield c
    c.close()


@pytest.fixture
def http(monkeypatch):
    queue = []
    seen = []

    def send(session, request, **kwargs):
        seen.append(request)
        assert request.method == "GET"
        status, body = queue.pop(0)
        r = requests.Response()
        r.status_code = status
        r.raw = io.BytesIO(json.dumps(body).encode())
        r.url = request.url
        return r

    monkeypatch.setattr(requests.Session, "send", send)
    return queue, seen


def test_paginated_short_and_empty_pages_preserve_large_integers(client, http):
    q, seen = http
    q.extend(
        [
            (
                200,
                {
                    "records": [{"uuid": "v1", "space": {"used": 9007199254740993}}],
                    "_links": {"next": {"href": "/api/storage/volumes?start=1"}},
                },
            ),
            (200, {"records": [], "_links": {"next": {"href": "/api/storage/volumes?start=2"}}}),
            (200, {"records": [{"uuid": "v2", "space": {"used": None}}]}),
        ]
    )
    df = client.execute_query({"table": "volumes", "fields": ["uuid", "space.used"]})
    assert len(df) == 2
    assert df.iloc[0]["space.used"] == 9007199254740993
    assert str(df["space.used"].dtype) == "Int64"
    assert df["_bow_query_id"].nunique() == 1
    assert all(parse_qs(urlsplit(r.url).query)["is_constituent"] == ["false"] for r in seen)


@pytest.mark.parametrize(
    "href",
    ["https://evil.example/api/storage/volumes", "/api/security/accounts", "/api/storage/volumes?is_constituent=true"],
)
def test_continuations_cannot_change_origin_resource_or_scope(client, http, href):
    http[0].append((200, {"records": [], "_links": {"next": {"href": href}}}))
    with pytest.raises(OntapQueryError) as e:
        client.execute_query({"table": "volumes"})
    assert e.value.code == "InvalidResponse"
    assert len(http[1]) == 1


@pytest.mark.parametrize(
    "status,code", [(401, "AuthenticationFailed"), (403, "PermissionDenied"), (404, "UnsupportedResource")]
)
def test_permissions_and_unsupported_are_not_empty(client, http, status, code):
    http[0].append((status, {"error": {"message": "sensitive customer text"}}))
    with pytest.raises(OntapQueryError) as e:
        client.execute_query({"table": "volumes"})
    assert e.value.code == code
    assert "sensitive" not in str(e.value)
    assert len(http[1]) == 1


@pytest.mark.parametrize(
    "query",
    [
        {"table": "volumes", "path": "/api/cluster"},
        {"table": "volumes", "fields": ["password"]},
        {"table": "volumes", "filter": {"return_records": False}},
        {"table": "volumes", "filter": {"is_constituent": True}},
        {"table": "volumes", "limit": True},
        {"table": "volume_metrics", "parent": {"volume.uuid": "../../cluster"}, "lookback": "1h"},
        {
            "table": "diag_protocols_cifs_domains_svm_uuid",
            "parent": {"svm.uuid": "s"},
            "filter": {"rediscover_trusts": True},
        },
        {"table": "diag_storage_luns_uuid", "parent": {"uuid": "l"}, "filter": {"data.offset": 0}},
    ],
)
def test_invalid_or_unsafe_queries_make_no_http_request(client, http, query):
    with pytest.raises(OntapQueryError):
        client.execute_query(query)
    assert not http[1]


def test_complete_result_limit_never_silently_truncates(client, http):
    http[0].extend(
        [
            (200, {"records": [{"uuid": "a"}], "_links": {"next": {"href": "/api/storage/volumes?start=1"}}}),
            (200, {"records": [{"uuid": "b"}]}),
        ]
    )
    with pytest.raises(OntapQueryError) as e:
        client.execute_query({"table": "volumes", "limit": 1})
    assert e.value.code == "ResultLimitExceeded"


def test_partial_error_does_not_become_usable_data(client, http):
    http[0].append((200, {"records": [{"uuid": "a"}], "errors": [{"message": "partial"}]}))
    with pytest.raises(OntapQueryError) as e:
        client.execute_query({"table": "volumes"})
    assert e.value.code == "PartialResponse"


def test_history_tier_uses_age_of_start_not_incident_duration(client, http):
    start = datetime.now(UTC) - timedelta(days=6)
    http[0].append((200, {"records": []}))
    df = client.execute_query(
        {
            "table": "volume_metrics",
            "parent": {"volume.uuid": "abc"},
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(minutes=10)).isoformat(),
        }
    )
    assert parse_qs(urlsplit(http[1][0].url).query)["interval"] == ["1w"]
    assert {"timestamp", "duration", "status", "volume.uuid"} <= set(df.columns)


def test_requested_history_resolution_cannot_invent_retention(client, http):
    with pytest.raises(OntapQueryError) as e:
        client.execute_query(
            {"table": "volume_metrics", "parent": {"volume.uuid": "abc"}, "lookback": "6d", "interval": "1h"}
        )
    assert e.value.code == "UnavailableHistory"
    assert not http[1]


def test_diagnostic_parent_table_uses_same_executor(client, http):
    table = next(
        t
        for t in client.get_tables()
        if t.metadata_json
        and t.metadata_json["netapp"]["endpoint"] == "/api/protocols/nfs/export-policies/{policy.id}/rules"
    )
    http[0].append((200, {"records": [{"index": 4}]}))
    df = client.execute_query({"table": table.name, "parent": {"policy.id": "17"}, "fields": ["index"]})
    assert df.iloc[0]["index"] == 4
    assert df.iloc[0]["policy.id"] == "17"
    assert "/export-policies/17/rules?" in http[1][0].url


def test_collection_schema_is_available_when_empty_without_device_calls(client, http):
    a = client.get_schema("volumes")
    assert a.columns and not http[1]
    http[0].append((200, {"records": []}))
    df = client.execute_query({"table": "volumes", "fields": ["space.used"]})
    assert df.empty and str(df["space.used"].dtype) == "Int64"


def test_logical_memberships_do_not_duplicate_capacity(client, http):
    http[0].append((200, {"records": [{"uuid": "v", "aggregates": [{"uuid": "a"}, {"uuid": "b"}]}]}))
    df = client.execute_query({"table": "volume_aggregates"})
    assert len(df) == 2 and df["volume.uuid"].nunique() == 1
    assert not any(c.startswith("space.") for c in df.columns)


def test_cluster_connection_checks_version(client, http):
    http[0].append(
        (
            200,
            {
                "uuid": "c",
                "name": "lab",
                "version": {"generation": 9, "major": 14, "minor": 1, "full": "NetApp Release 9.14.1P9"},
            },
        )
    )
    assert client.test_connection()["success"]


def test_all_swagger_gets_have_coverage_dispositions(client):
    report = client.coverage_report()
    assert len(report["resources"]) == 504
    assert len({r["path"] for r in report["resources"]}) == 504
    assert all(r["reason"] for r in report["resources"])
    assert all(r["availability"] == "unverified" for r in report["resources"])
    assert not any(
        "/passwords/" in t.metadata_json["netapp"]["endpoint"] for t in client.get_tables() if t.metadata_json
    )


def test_singleton_queries_do_not_send_collection_controls(client, http):
    http[0].append((200, {"uuid": "cluster"}))
    client.execute_query({"table": "cluster", "fields": ["uuid"]})
    params = parse_qs(urlsplit(http[1][0].url).query)
    assert set(params) == {"fields"}


@pytest.mark.parametrize("bad", [[], {}, None, 42])
def test_explicit_invalid_projection_is_not_replaced_by_defaults(client, http, bad):
    with pytest.raises(OntapQueryError):
        client.execute_query({"table": "volumes", "fields": bad})
    assert not http[1]


def test_nested_unexpected_secret_values_are_not_returned(client, http):
    http[0].append(
        (
            200,
            {
                "records": [
                    {
                        "uuid": "a",
                        "aggregates": [{"uuid": "x", "password": "sensitive", "nested": {"secret_key": "sensitive"}}],
                    }
                ]
            },
        )
    )
    df = client.execute_query({"table": "volumes", "fields": ["aggregates"]})
    assert "sensitive" not in df.to_json()
    assert df.iloc[0]["aggregates"][0]["uuid"] == "x"


def test_transient_error_retries_then_returns_complete_data(client, http, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    http[0].extend([(503, {}), (200, {"records": [{"uuid": "ok"}]})])
    df = client.execute_query({"table": "volumes"})
    assert len(df) == 1 and len(http[1]) == 2


def test_unsupported_version_is_not_reported_connected(client, http):
    http[0].append((200, {"uuid": "a", "version": {"generation": 9, "major": 16}}))
    with pytest.raises(OntapQueryError) as e:
        client.test_connection()
    assert e.value.code == "UnsupportedVersion"


def test_query_alias_and_async_wrapper_use_same_contract(client, http):
    import asyncio

    http[0].extend([(200, {"records": []}), (200, {"records": []})])
    assert client.query({"table": "volumes"}).empty
    assert asyncio.run(client.aexecute_query({"table": "volumes"})).empty


def test_projection_preserves_counter_arrays_without_row_explosion(client, http):
    http[0].append(
        (200, {"records": [{"id": "row-a", "counters": [{"name": "a", "value": 123}, {"name": "b", "value": 234}]}]})
    )
    df = client.execute_query(
        {
            "table": "diag_cluster_counter_tables_counter_table_name_rows",
            "parent": {"counter_table.name": "volume"},
            "fields": ["id", "counters"],
        }
    )
    assert len(df) == 1 and len(df.iloc[0]["counters"]) == 2


@pytest.mark.parametrize("table,expected", [("volumes", "false"), ("volume_constituents", "true")])
def test_volume_grains_are_separate(client, http, table, expected):
    http[0].append((200, {"records": []}))
    client.execute_query({"table": table})
    assert parse_qs(urlsplit(http[1][0].url).query)["is_constituent"] == [expected]
    with pytest.raises(OntapQueryError, match="logical volumes"):
        client.execute_query({"table": table, "filter": {"is_constituent": expected != "true"}})


@pytest.mark.parametrize("query", ["refresh=true", "name=other", "start=1&start=2"])
def test_continuation_rejects_added_controls_and_duplicate_keys(client, http, query):
    http[0].append((200, {"records": [], "_links": {"next": {"href": "/api/storage/volumes?" + query}}}))
    with pytest.raises(OntapQueryError, match="unsupported query controls"):
        client.execute_query({"table": "volumes"})


def test_rca_query_runs_through_bow_code_executor(client, http):
    from app.ai.code_execution.code_execution import StreamingCodeExecutor

    http[0].append(
        (
            200,
            {
                "records": [
                    {"uuid": "a", "name": "a", "space": {"used": 20, "size": 100}},
                    {"uuid": "b", "name": "b", "space": {"used": 80, "size": 100}},
                ]
            },
        )
    )
    code = """
def generate_df(ds_clients, excel_files):
    df = ds_clients["ontap"].execute_query({"table": "volumes", "fields": ["name", "space.used", "space.size"]})
    df["used_percent"] = 100 * df["space.used"] / df["space.size"]
    return df.sort_values("used_percent", ascending=False)
"""
    client._bow_connection_id = "synthetic-connection"
    frame, _, queries = StreamingCodeExecutor().execute_code(code=code, ds_clients={"ontap": client}, excel_files=[])
    assert list(frame["used_percent"]) == [80, 20]
    assert len(queries) == 1
    assert json.loads(queries[0])["table"] == "volumes"
    assert set(frame["_bow_connection"]) == {"synthetic-connection"}
    assert len(http[1]) == 1


@pytest.mark.parametrize(
    "query",
    [
        {"table": "volumes", "filter": {"name": {"op": [], "value": "x"}}},
        {"table": "volume_aggregates", "fields": [[]]},
        {"table": "volume_metrics", "parent": {"volume.uuid": "v"}, "lookback": "1h", "interval": []},
    ],
)
def test_malformed_query_values_are_typed_errors(client, http, query):
    with pytest.raises(OntapQueryError, match="InvalidQuery"):
        client.execute_query(query)
    assert not http[1]


def test_catalog_parents_bind_every_placeholder(client):
    import re

    for table in client.get_tables():
        meta = (table.metadata_json or {}).get("netapp")
        if meta:
            assert set(re.findall(r"\{([^}]+)\}", meta["endpoint"])) == set(meta["parents"]), table.name


def test_all_history_tables_have_time_filters(client):
    for table in client.get_tables():
        meta = (table.metadata_json or {}).get("netapp")
        if meta and meta["history"]:
            assert {"timestamp", "time"} & meta["filters"].keys(), table.name


def test_nfs_history_retains_nested_quality_and_time(client, http):
    http[0].append(
        (
            200,
            {
                "records": [
                    {
                        "v4": {
                            "timestamp": "2026-01-01T00:00:00Z",
                            "duration": "PT15S",
                            "status": "ok",
                            "iops": {"total": 8},
                        }
                    }
                ]
            },
        )
    )
    frame = client.execute_query(
        {
            "table": "diag_protocols_nfs_services_svm_uuid_metrics",
            "parent": {"svm.uuid": "svm-a"},
            "lookback": "1h",
            "fields": ["v4.iops.total"],
        }
    )
    assert {"v4.timestamp", "v4.duration", "v4.status"} <= set(frame.columns)
    assert "timestamp" in parse_qs(urlsplit(http[1][0].url).query)
    assert frame.iloc[0]["v4.iops.total"] == 8


@pytest.mark.parametrize("table", ["diag_security_ssh", "diag_security_authentication_cluster_ad_proxy"])
def test_queries_only_send_declared_controls(client, http, table):
    http[0].append((200, {"records": []}))
    client.execute_query({"table": table})
    assert not parse_qs(urlsplit(http[1][0].url).query)


@pytest.mark.parametrize("identity", [None, "", "   "])
def test_missing_cluster_identity_is_rejected(client, http, identity):
    http[0].append((200, {"uuid": identity, "version": {"generation": 9, "major": 14, "minor": 1}}))
    with pytest.raises(OntapQueryError) as error:
        client.test_connection()
    assert error.value.code == "InvalidResponse"


@pytest.mark.parametrize(
    "table,records",
    [
        ("volumes", []),
        ("volumes", [{"uuid": "a"}]),
        ("volume_aggregates", []),
        ("volume_aggregates", [{"uuid": "a", "aggregates": [{"uuid": "b"}]}]),
    ],
)
def test_retrieval_timestamp_supports_datetime_operations(client, http, table, records):
    http[0].append((200, {"records": records}))
    frame = client.execute_query({"table": table})
    assert str(frame["_bow_retrieved_at"].dtype) == "datetime64[ns, UTC]"
    assert len(frame["_bow_retrieved_at"].dt.floor("h")) == len(frame)


def test_invalid_integer_parent_rejected_before_http(client, http):
    with pytest.raises(OntapQueryError) as error:
        client.execute_query(
            {"table": "diag_cluster_sensors_node_uuid_index", "parent": {"node.uuid": "n", "index": "not-a-number"}}
        )
    assert error.value.code == "InvalidQuery"
    assert not http[1]


def test_every_advertised_rest_table_can_construct_a_bounded_request(client, http):
    from urllib.parse import quote

    for table in client.get_tables():
        meta = (table.metadata_json or {}).get("netapp")
        if not meta:
            continue
        parents = {}
        for key, contract in meta["parent_types"].items():
            value = (contract.get("enum") or [None])[0]
            if value is None:
                value = (
                    max(0, int(contract.get("minimum", 0)))
                    if contract["type"] == "integer" or any(c.name == key and c.dtype == "int" for c in table.columns)
                    else "synthetic-parent"
                )
            parents[key] = str(value)
        query = {"table": table.name, "parent": parents}
        if meta["history"]:
            query["lookback"] = "1h"
        http[0].append((200, {"records": []} if meta["collection"] else {}))
        frame = client.execute_query(query)
        request = http[1][-1]
        expected = meta["endpoint"]
        for key, value in parents.items():
            expected = expected.replace("{" + key + "}", quote(value, safe=""))
        assert urlsplit(request.url).path == expected, table.name
        assert set(parse_qs(urlsplit(request.url).query)) <= set(meta["controls"]) | set(meta["filters"]), table.name
        assert str(frame["_bow_retrieved_at"].dtype) == "datetime64[ns, UTC]"


@pytest.mark.parametrize("change", ["missing_parent", "extra_parent", "broken_template", "missing_time_filter"])
def test_malformed_catalog_is_rejected_before_any_http(client, http, monkeypatch, change):
    from pathlib import Path

    from app.data_sources.clients.netapp_ontap_client import _catalog

    original = Path.read_text
    catalog = json.loads(original(Path(__file__).parents[2] / "app/data_sources/clients/netapp/catalog.json"))
    resource = catalog["resources"]["volume_metrics"]
    if change == "missing_parent":
        resource["parents"] = []
    elif change == "extra_parent":
        resource["parents"].append("extra")
    elif change == "broken_template":
        resource["path"] += "{"
    else:
        resource["filters"].pop("timestamp")

    def read(path, *args, **kwargs):
        return (
            json.dumps(catalog)
            if path.name == "catalog.json" and path.parent.name == "netapp"
            else original(path, *args, **kwargs)
        )

    monkeypatch.setattr(Path, "read_text", read)
    _catalog.cache_clear()
    try:
        with pytest.raises(OntapQueryError) as error:
            client.get_tables()
        assert error.value.code == "InvalidCatalog"
        assert not http[1]
    finally:
        _catalog.cache_clear()

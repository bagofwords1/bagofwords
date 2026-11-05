"""VCenterClient against the vim25 mock over real HTTPS.

The boundary here is the network: the mock (tools/vcenter/mock_vcenter.py)
decodes requests and encodes responses with pyVmomi's own SOAP serializer, so
these tests exercise the client's real pyVmomi + requests code paths: TLS with
a private CA, version negotiation, Login/Logout, ContainerView + paged
RetrievePropertiesEx, PerformanceManager, event/task collectors and the REST
tagging API. Expected values are read from the mock's own seeded state, not
hard-coded, so the assertions are about the contract (complete, correctly
mapped, no leaked sessions), not about one scenario.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import pickle
import re
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

pytest.importorskip("pyVmomi")

from app.data_sources.clients.vcenter_client import _CATALOG, VCenterClient  # noqa: E402

MOCK_PATH = Path(__file__).resolve().parents[3] / "tools" / "vcenter" / "mock_vcenter.py"


@contextmanager
def _env(**overrides):
    old = {k: os.environ.get(k) for k in overrides}
    try:
        for k, v in overrides.items():
            os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _start_mock(**env):
    """Load a fresh copy of the mock (its estate is built at import) and serve it on a free port."""
    with _env(**env):
        spec = importlib.util.spec_from_file_location(f"vcenter_mock_{uuid.uuid4().hex[:8]}", MOCK_PATH)
        mock = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mock)
    server, ca = mock.serve("127.0.0.1", 0, True)
    return mock, server, ca


@pytest.fixture(scope="module")
def vc():
    mock, server, ca = _start_mock()
    yield mock, server.server_address[1], ca
    server.shutdown()


def _client(vc, **kw):
    mock, port, ca = vc
    args = dict(host="localhost", port=port, username=mock.USER, password=mock.PASSWORD, ca_certificate=ca)
    args.update(kw)
    return VCenterClient(**args)


def _assert_no_leaks(mock):
    assert mock.SESSIONS == {}, "SOAP sessions left open"
    assert mock.REST_SESSIONS == {}, "REST sessions left open"
    assert mock.VIEWS == {} and mock.COLLECTORS == {}, "views / collectors not destroyed"


# ── connection ────────────────────────────────────────────────────────────────

def test_connection_reports_estate_and_logs_out(vc):
    mock = vc[0]
    result = _client(vc).test_connection()
    assert result["success"], result["message"]
    vms = sum(1 for o in mock.OBJECTS.values() if o.cls is mock.vim.VirtualMachine)
    assert f"{vms} VMs" in result["message"]
    _assert_no_leaks(mock)


def test_bad_password_fails_cleanly(vc):
    result = _client(vc, password="wrong-" + uuid.uuid4().hex[:6]).test_connection()
    assert result["success"] is False
    assert "rejected the login" in result["message"]


def test_untrusted_certificate_is_refused_unless_trusted_or_verification_off(vc):
    no_ca = _client(vc, ca_certificate=None).test_connection()
    assert no_ca["success"] is False and "CA certificate" in no_ca["message"]
    assert _client(vc, ca_certificate=None, verify_ssl=False).test_connection()["success"]


@pytest.mark.parametrize("host", ["localhost", "https://localhost", "https://localhost/ui", "localhost/sdk"])
def test_host_accepts_names_and_urls(vc, host):
    assert _client(vc, host=host).test_connection()["success"]


def test_plain_http_url_is_rejected():
    with pytest.raises(ValueError):
        VCenterClient(host="http://vc.example.com", username="u", password="p")


# ── catalog contract ──────────────────────────────────────────────────────────

_SPECS = {
    "metrics_available": {"entity_type": "VirtualMachine", "name": "prod-db-01"},
    "metrics": {"entity_type": "VirtualMachine", "names": ["prod-db-01", "web-01"],
                "counters": ["cpu.usage.average", "disk.maxTotalLatency.latest"], "minutes": 60},
    "events": {"hours": 24 * 30},
    "tasks": {"hours": 24 * 30},
}


def test_every_table_returns_its_declared_columns_and_rows(vc):
    client = _client(vc)
    schemas = {t.name for t in client.get_schemas()}
    assert schemas == set(_CATALOG)
    for table in _CATALOG:
        df = client.execute_query({"table": table, **_SPECS.get(table, {})})
        assert list(df.columns) == [c for c, _, _ in _CATALOG[table]["columns"]], table
        assert not df.empty, table
        # Results leave the code-execution sandbox pickled: no pyVmomi types may survive.
        pickle.loads(pickle.dumps(df))
        for col in df.columns:
            leaked = {type(v).__module__ for v in df[col] if v is not None and "pyVmomi" in type(v).__module__}
            assert not leaked, f"{table}.{col} holds pyVmomi objects"
    _assert_no_leaks(vc[0])


def test_inventory_is_complete_across_property_collector_pages(vc, monkeypatch):
    mock = vc[0]
    monkeypatch.setattr(mock, "PAGE_CAP", 4)            # the server pages harder than the client asked
    df = _client(vc).execute_query({"table": "vms"})
    expected = {o.props["name"] for o in mock.OBJECTS.values() if o.cls is mock.vim.VirtualMachine}
    assert set(df["name"]) == expected and len(df) == len(expected)
    assert mock.CALLS.get("ContinueRetrievePropertiesEx", 0) > 0
    _assert_no_leaks(mock)


def test_vm_placement_matches_inventory(vc):
    mock = vc[0]
    df = _client(vc).execute_query({"table": "vms"}).set_index("moid")
    for moid, row in df.iterrows():
        mo = mock.OBJECTS[moid]
        host = mock.OBJECTS[mo.props["runtime"].host._moId]
        assert row["host"] == host.props["name"]
        ds_names = {mock.OBJECTS[d._moId].props["name"] for d in mo.props["datastore"]}
        assert set(row["datastores"].split(", ")) == ds_names
        assert row["num_cpu"] == mo.props["config"].hardware.numCPU
        assert bool(row["snapshot_count"]) == (mo.props["snapshot"] is not None)
        assert (row["snapshot_size_gb"] > 0) == (mo.props["snapshot"] is not None)


def test_scope_filters_narrow_inventory(vc):
    client = _client(vc)
    every = client.execute_query({"table": "vms"})
    on = client.execute_query({"table": "vms", "power_state": "poweredOn"})
    assert 0 < len(on) < len(every) and set(on["power_state"]) == {"poweredOn"}
    cluster = every["cluster"].dropna().iloc[0]
    scoped = client.execute_query({"table": "vms", "cluster": cluster})
    assert len(scoped) == (every["cluster"] == cluster).sum()
    some = sorted(every["name"])[:2]
    assert set(client.execute_query({"table": "vms", "names": some})["name"]) == set(some)
    rx = client.execute_query({"table": "vms", "name_regex": "^" + re.escape(some[0][:3])})
    assert set(rx["name"]) == {n for n in every["name"] if n.lower().startswith(some[0][:3].lower())}
    cols = client.execute_query({"table": "hosts", "columns": ["name", "cluster"]})
    assert list(cols.columns) == ["name", "cluster"]


def test_every_triggered_alarm_is_listed_once_with_names_resolved(vc):
    mock = vc[0]
    df = _client(vc).execute_query({"table": "alarms"})
    assert len(df) == len(mock.TRIGGERED) == df["key"].nunique()
    assert set(df["status"]) <= {"red", "yellow"}
    assert set(df["alarm"]) == {mock.OBJECTS[st.alarm._moId].props["info"].name for st in mock.TRIGGERED}
    assert set(df["entity"]) == {mock.OBJECTS[st.entity._moId].props["name"] for st in mock.TRIGGERED}


def test_license_keys_are_masked(vc):
    mock = vc[0]
    df = _client(vc).execute_query({"table": "licenses"})
    for lic in mock.LICENSES:
        assert lic.licenseKey not in set(df["license_key"])
        assert lic.licenseKey[-5:] in "".join(df["license_key"])


def test_tags_join_objects_by_moid_and_name(vc):
    mock = vc[0]
    df = _client(vc).execute_query({"table": "tags"})
    assert len(df) == sum(len(v) for v in mock.ASSOC.values())
    for _, r in df.iterrows():
        assert mock.OBJECTS[r["object_moid"]].props["name"] == r["object_name"]


# ── timeline ──────────────────────────────────────────────────────────────────

def test_events_are_complete_beyond_one_collector_page_and_newest_first(vc):
    mock = vc[0]
    df = _client(vc).execute_query({"table": "events", "hours": 24 * 8})
    begin = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24 * 8)
    expected = [e for e in mock.EVENTS if e.createdTime >= begin]
    assert len(expected) > mock.COLLECTOR_MAX              # needs more than one ReadNextEvents page
    assert len(df) == len(expected)
    assert df["created"].is_monotonic_decreasing


def test_event_filters(vc):
    client = _client(vc)
    window = {"hours": 24 * 30}
    warn = client.execute_query({"table": "events", "category": ["warning", "error"], **window})
    assert not warn.empty and set(warn["category"]) <= {"warning", "error"}
    esx = client.execute_query({"table": "events", "event_type": "esx.problem.*", **window})
    assert not esx.empty and esx["event_type"].str.startswith("esx.problem.").all()
    vm = warn.dropna(subset=["vm"])["vm"].iloc[0]
    scoped = client.execute_query({"table": "events", "entity": vm, "entity_type": "vm", **window})
    assert not scoped.empty and (scoped["vm"] == vm).all()
    newest = client.execute_query({"table": "events", "limit": 3, **window})
    assert len(newest) == 3 and newest["created"].iloc[0] == client.execute_query({"table": "events", **window})["created"].max()


def test_tasks_carry_state_user_and_error_text(vc):
    mock = vc[0]
    df = _client(vc).execute_query({"table": "tasks", "hours": 24 * 30})
    assert len(df) == len(mock.TASKS)
    failed = df[df["state"] == "error"]
    assert not failed.empty and failed["error"].str.len().gt(20).all()
    assert not failed["error"].str.contains("dynamicType").any()        # a message, not a fault repr
    only_err = _client(vc).execute_query({"table": "tasks", "hours": 24 * 30, "state": "error"})
    assert set(only_err["key"]) == set(failed["key"])


# ── performance ───────────────────────────────────────────────────────────────

def test_metric_values_match_server_samples_with_percent_scaled(vc):
    mock = vc[0]
    df = _client(vc).execute_query(_SPECS["metrics"] | {"table": "metrics"})
    assert set(df["interval_seconds"]) == {20}                         # last hour of a VM -> realtime
    for _, r in df.sample(20, random_state=1).iterrows():
        cid = mock.BY_KEY[r["counter"]]
        raw = mock._value(r["moid"], cid, r["instance"], r["timestamp"].to_pydatetime(), 20)
        expected = raw / 100 if mock.COUNTERS[cid]["unit"] == "percent" else raw
        assert r["value"] == pytest.approx(expected)
    cpu = df[df["counter"] == "cpu.usage.average"]["value"]
    assert cpu.between(0, 100).all()


def test_auto_interval_uses_historical_rollups_for_datastores_and_old_windows(vc):
    client = _client(vc)
    ds = client.execute_query({"table": "metrics", "entity_type": "Datastore", "name": "ds-prod-01",
                               "counters": ["disk.used.latest"], "hours": 24 * 7})
    assert not ds.empty and set(ds["interval_seconds"]) == {1800}
    host = client.execute_query({"table": "metrics", "entity_type": "host", "name_regex": ".",
                                 "counters": ["cpu.usage.average"], "hours": 6})
    assert set(host["interval_seconds"]) == {300}


def test_historical_queries_are_batched_under_max_query_metrics(vc, monkeypatch):
    mock = vc[0]
    monkeypatch.setattr(mock, "MAX_QUERY_METRICS", 3)        # a stricter vpxd.stats.maxQueryMetrics
    names = sorted(o.props["name"] for o in mock.OBJECTS.values()
                   if o.cls is mock.vim.VirtualMachine and o.props["runtime"].powerState == "poweredOn")[:5]
    df = _client(vc).execute_query({"table": "metrics", "entity_type": "vm", "names": names,
                                    "counters": ["cpu.usage.average", "mem.usage.average"], "hours": 6})
    assert set(df["entity"]) == set(names)
    assert set(df["counter"]) == {"cpu.usage.average", "mem.usage.average"}


def test_counters_above_the_statistics_level_get_an_actionable_error(vc):
    with pytest.raises(ValueError, match="statistics level"):
        _client(vc).execute_query({"table": "metrics", "entity_type": "vm", "name": "prod-db-01",
                                   "counters": ["virtualDisk.totalWriteLatency.average"], "hours": 6})


def test_per_instance_realtime_metrics(vc):
    df = _client(vc).execute_query({"table": "metrics", "entity_type": "vm", "name": "prod-db-01",
                                    "counters": ["virtualDisk.totalWriteLatency.average"], "instance": "*",
                                    "minutes": 30})
    assert {"", "scsi0:0", "scsi0:1"} <= set(df["instance"])


# ── spec validation ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("spec, message", [
    ({"table": "nope"}, "Unknown vCenter table"),
    ({"table": "vms", "bogus": 1}, "Unsupported fields"),
    ({"table": "vms", "columns": ["nope"]}, "Unknown columns"),
    ({"table": "vms", "limit": 0}, "positive integer"),
    ({"table": "metrics", "entity_type": "vm", "name": "prod-db-01"}, "counters"),
    ({"table": "metrics", "entity_type": "vm", "name": "prod-db-01", "counters": ["cpu.usage.avg"]}, "Unknown counter"),
    ({"table": "metrics", "counters": ["cpu.usage.average"]}, "entity_type"),
    ({"table": "metrics", "entity_type": "Datastore", "name": "ds-prod-01", "counters": ["disk.used.latest"],
      "interval": 20}, "Realtime"),
    ({"table": "events", "start_time": "not a time"}, "ISO-8601"),
])
def test_invalid_specs_explain_themselves(vc, spec, message):
    with pytest.raises(ValueError, match=message):
        _client(vc).execute_query(spec)
    _assert_no_leaks(vc[0])


def test_query_accepts_json_strings(vc):
    df = _client(vc).execute_query('{"table": "about"}')
    assert df.loc[0, "api_type"] == "VirtualCenter"


# ── other endpoint flavours ──────────────────────────────────────────────────

def test_standalone_esxi_degrades_without_failing():
    mock, server, ca = _start_mock(VCENTER_MOCK_MODE="esxi")
    try:
        client = VCenterClient(host="localhost", port=server.server_address[1], username=mock.USER,
                               password=mock.PASSWORD, ca_certificate=ca)
        res = client.test_connection()
        assert res["success"] and "standalone ESXi" in res["message"]
        assert client.execute_query({"table": "clusters"}).empty
        assert client.execute_query({"table": "tags"}).empty
        assert not client.execute_query({"table": "vms"}).empty
        m = client.execute_query({"table": "metrics", "entity_type": "vm", "name_regex": ".",
                                  "counters": ["cpu.usage.average"], "minutes": 30})
        assert set(m["interval_seconds"]) == {20}
        _assert_no_leaks(mock)
    finally:
        server.shutdown()


@pytest.mark.parametrize("api", ["7.0.3.0", "8.0.3.0"])
def test_older_vim25_api_versions_negotiate(api):
    mock, server, ca = _start_mock(VCENTER_MOCK_API_VERSION=api)
    try:
        client = VCenterClient(host="localhost", port=server.server_address[1], username=mock.USER,
                               password=mock.PASSWORD, ca_certificate=ca)
        assert client.execute_query({"table": "about"}).loc[0, "api_version"] == api
        assert not client.execute_query({"table": "vms"}).empty
        assert mock.VERSIONS_SEEN == {f'"urn:vim25/{api}"'} or mock.VERSIONS_SEEN == {f"urn:vim25/{api}"}
    finally:
        server.shutdown()

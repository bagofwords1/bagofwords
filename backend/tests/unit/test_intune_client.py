"""Client-to-ASGI contract checks for the Microsoft Intune connector.

`IntuneClient` is driven against tools/intune/mock_graph.py — a doc-shaped
stand-in for Microsoft Graph (token endpoint + the Intune collections) — served
in-process through `TestClient`, so the real HTTP, paging, OData and throttle
code runs with only the network boundary replaced. Expected values are derived
from the mock's own seeded data, never pasted from output.

The simulator is an external boundary, not evidence of tenant compatibility:
Loop B in docs/feedback-loops/intune-connector.md covers a real tenant.
"""
import importlib.util
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.data_sources.clients import intune_client as intune_mod
from app.data_sources.clients.intune_client import TABLES, IntuneClient
from app.schemas.data_source_registry import REGISTRY, resolve_client_class

_MOCK_PATH = Path(__file__).resolve().parents[3] / "tools/intune/mock_graph.py"


@pytest.fixture
def graph(monkeypatch):
    spec = importlib.util.spec_from_file_location("intune_graph_mock", _MOCK_PATH)
    mock = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mock)
    # Retry-After waits are real seconds; the throttle contract is about
    # retrying, not about wall-clock time.
    monkeypatch.setattr("app.data_sources.clients.graph_drive_client.time.sleep", lambda s: None)
    with TestClient(mock.app) as http:
        def make(app_id="intune-reader", secret=None, tenant=None, **kw):
            # Unique tenant per client: Graph throttle state is process-wide
            # per (tenant, app), and must not leak between tests.
            client = IntuneClient(
                tenant_id=tenant or str(uuid.uuid4()),
                client_id=app_id,
                client_secret=secret if secret is not None else mock.APPS[app_id][0],
                **kw,
            )
            # Pin the in-process transport. Assigning `_http` alone is not
            # enough: a collected client's __del__ closes it, and the next
            # client would then silently build a real httpx client and reach
            # the real login.microsoftonline.com.
            client._client = lambda: http
            client.close = lambda: None
            return client
        yield mock, make


def _readable(mock, app_id):
    perms = mock.APPS[app_id][1]
    return {name for name, meta in TABLES.items() if meta["permission"] in perms}


# ── registry ──────────────────────────────────────────────────────────────────

def test_registry_resolves_intune_client_with_app_only_auth():
    assert resolve_client_class("intune") is IntuneClient
    entry = REGISTRY["intune"]
    variants = entry.credentials_auth.by_auth
    assert set(variants) == {"service_principal"}
    # Tenant-wide inventory: no per-user credential variant.
    assert variants["service_principal"].scopes == ["system"]


# ── connection test ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("app_id", ["intune-reader", "intune-partial"])
def test_connection_succeeds_when_any_table_is_readable_and_reports_gaps(graph, app_id):
    mock, make = graph
    result = make(app_id).test_connection()
    assert result["success"] is True
    assert set(result["details"]["readable_tables"]) == _readable(mock, app_id)
    missing = result["details"]["missing_permissions"]
    expected_missing = {m["permission"] for n, m in TABLES.items() if n not in _readable(mock, app_id)}
    assert set(missing) == expected_missing


@pytest.mark.parametrize("kwargs", [
    {"app_id": "intune-none"},                       # token OK, nothing granted
    {"app_id": "intune-reader", "secret": "wrong"},  # bad secret
    {"app_id": "intune-reader", "tenant": "nolicense"},  # tenant without Intune
])
def test_connection_fails_when_no_intune_data_is_reachable(graph, kwargs):
    _, make = graph
    assert make(**kwargs).test_connection()["success"] is False


# ── catalog ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("app_id", ["intune-reader", "intune-partial", "intune-none"])
def test_catalog_is_exactly_the_tables_the_app_may_read(graph, app_id):
    mock, make = graph
    tables = make(app_id).get_schemas()
    assert {t.name for t in tables} == _readable(mock, app_id)
    for t in tables:
        assert [c.name for c in t.columns] == [c for c, _ in TABLES[t.name]["columns"]]


def test_transient_throttling_never_drops_tables_or_rows(graph):
    mock, make = graph
    baseline = make().execute_query({"table": "managed_devices"})
    mock.THROTTLE_EVERY = 2  # every 2nd Graph call is a 429 + Retry-After
    try:
        client = make()
        assert {t.name for t in client.get_schemas()} == set(TABLES)
        throttled = client.execute_query({"table": "managed_devices"})
    finally:
        mock.THROTTLE_EVERY = 0
    assert len(throttled) == len(baseline) == len(mock.DEVICES)


def test_non_permission_failure_surfaces_instead_of_shrinking_catalog(graph):
    mock, make = graph
    mock.FAIL_COLLECTIONS.add("deviceManagement/detectedApps")
    try:
        with pytest.raises(ValueError):
            make().get_schemas()
    finally:
        mock.FAIL_COLLECTIONS.clear()


# ── queries ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("page_size", [7, 50, 500])
def test_query_pages_through_every_record(graph, monkeypatch, page_size):
    mock, make = graph
    monkeypatch.setattr(intune_mod, "PAGE_SIZE", page_size)
    df = make().execute_query({"table": "managed_devices", "select": ["id"]})
    assert sorted(df["id"]) == sorted(d["id"] for d in mock.DEVICES)


@pytest.mark.parametrize("state", ["compliant", "noncompliant", "inGracePeriod", "unknown"])
def test_filter_is_applied_server_side_and_matches_source(graph, state):
    mock, make = graph
    df = make().execute_query({
        "table": "managed_devices",
        "filter": f"complianceState eq '{state}'",
        "select": ["deviceName", "complianceState"],
    })
    expected = sorted(d["deviceName"] for d in mock.DEVICES if d["complianceState"] == state)
    assert sorted(df["deviceName"]) == expected
    assert list(df.columns) == ["deviceName", "complianceState"]


@pytest.mark.parametrize("limit", [1, 13, 119])
def test_limit_caps_rows(graph, limit):
    _, make = graph
    assert len(make().execute_query({"table": "managed_devices", "limit": limit})) == limit


def test_every_table_returns_its_declared_columns(graph):
    _, make = graph
    client = make()
    for name, meta in TABLES.items():
        df = client.execute_query({"table": name, "limit": 5})
        assert list(df.columns) == [c for c, _ in meta["columns"]], name
        assert len(df) > 0, name


def test_secrets_never_leave_the_connector(graph):
    mock, make = graph
    client = make()
    secrets = {d["activationLockBypassCode"] for d in mock.DEVICES if d["activationLockBypassCode"]}
    secrets |= {a["productKey"] for a in mock.AUTOPILOT if a["productKey"]}
    assert secrets  # the mock does carry them
    for name in TABLES:
        df = client.execute_query({"table": name, "limit": 50_000})
        values = {str(v) for v in df.to_numpy().ravel()}
        assert not (values & secrets), name


def test_never_sentinel_dates_come_back_as_null(graph):
    _, make = graph
    client = make()
    for name, meta in TABLES.items():
        df = client.execute_query({"table": name, "limit": 50_000})
        for col, dtype in meta["columns"]:
            if dtype != "datetime":
                continue
            vals = df[col].dropna().astype(str)
            assert not vals.str.startswith(("0001-01-01", "9999-12-31")).any(), f"{name}.{col}"


def test_device_statuses_union_every_policy_and_can_scope_to_one(graph):
    mock, make = graph
    client = make()
    everything = client.execute_query({"table": "compliance_policy_device_statuses", "limit": 50_000})
    assert len(everything) == sum(len(mock._statuses(p["id"])) for p in mock.POLICIES)
    assert set(everything["policyId"]) == {p["id"] for p in mock.POLICIES}

    target = mock.POLICIES[1]
    one = client.execute_query({
        "table": "compliance_policy_device_statuses",
        "policy": target["displayName"].upper(),  # name match is case-insensitive
    })
    assert set(one["policyId"]) == {target["id"]}
    assert len(one) == len(mock._statuses(target["id"]))


def test_unknown_policy_lists_real_ones(graph):
    mock, make = graph
    with pytest.raises(ValueError) as exc:
        make().execute_query({"table": "compliance_policy_device_statuses", "policy": "no-such-policy"})
    assert mock.POLICIES[0]["displayName"] in str(exc.value)


def test_unsupported_filter_tells_the_agent_to_fall_back_to_pandas(graph):
    _, make = graph
    with pytest.raises(ValueError) as exc:
        make().execute_query({"table": "managed_devices", "filter": "model eq 'Latitude 7440'"})
    assert "pandas" in str(exc.value)


def test_query_on_ungranted_table_names_the_missing_permission(graph):
    _, make = graph
    with pytest.raises(ValueError) as exc:
        make("intune-partial").execute_query({"table": "autopilot_devices"})
    assert TABLES["autopilot_devices"]["permission"] in str(exc.value)


@pytest.mark.parametrize("bad", [
    {"table": "not_a_table"},
    {"table": "managed_devices", "select": ["notAColumn"]},
    "{not json",
])
def test_invalid_specs_are_rejected(graph, bad):
    _, make = graph
    with pytest.raises(ValueError):
        make().execute_query(bad)


def test_json_string_and_bare_table_name_specs_are_equivalent(graph):
    _, make = graph
    client = make()
    a = client.execute_query('{"table": "detected_apps"}')
    b = client.execute_query("detected_apps")
    assert a.equals(b) and len(a) > 0


def test_agent_guide_names_every_table_and_its_filter_vocabulary():
    # Catalog table descriptions are not rendered to the agent, so the enum
    # vocabularies a filter must match ride in the connector's system prompt.
    guide = IntuneClient(tenant_id="t", client_id="c", client_secret="s").system_prompt()
    for name in TABLES:
        assert f"`{name}`" in guide
    assert "noncompliant" in guide and "nonCompliant" in guide

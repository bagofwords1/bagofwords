"""Unit tests for AzureCostClient (Azure Cost Management).

Covers the connector's contract without a live Azure tenant:
- scope normalization (GUID, path, ARM URL)
- query envelope -> Cost Management REST body (time window, grouping, filter,
  metric, cost type) and the documented API limits enforced client-side
- response shaping: columns/rows -> DataFrame, UsageDate parsing, nextLink paging
- throttling: 429 honours the Cost Management retry-after headers, gives up
  with an actionable error when the wait is too long
- PreTaxCost fallback when the default metric is rejected
- forecast + dimension_values tables, discovery fallback
- test_connection success / permission failure
- registry wiring

The HTTP session and the Entra ID token are faked; nothing leaves the process.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from app.data_sources.clients.azure_cost_client import (
    AzureCostClient,
    AzureCostError,
    normalize_scope,
)

SUB = "11111111-2222-3333-4444-555555555555"
TODAY = dt.date(2026, 10, 15)


# ---------- fakes ---------- #


class _Resp:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = "" if payload is None else str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class _Session:
    """Scripted HTTP session: pops one response per request, records calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "json": json})
        if not self.responses:
            raise AssertionError(f"unexpected request {method} {url}")
        r = self.responses.pop(0)
        return r(method, url, json) if callable(r) else r


class _Token:
    token = "fake-token"


class _Cred:
    def get_token(self, *scopes):
        return _Token()


def _client(responses, sleeps=None, **kw):
    session = _Session(responses)
    c = AzureCostClient(
        scope=kw.pop("scope", SUB),
        tenant_id="t", client_id="c", client_secret="s",
        _session=session,
        _sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
        _today=lambda: TODAY,
        **kw,
    )
    c._credential = _Cred()
    return c, session


def _query_payload(columns, rows, next_link=None):
    return {
        "properties": {
            "columns": [{"name": n, "type": t} for n, t in columns],
            "rows": rows,
            "nextLink": next_link,
        }
    }


# ---------- scope ---------- #


@pytest.mark.parametrize(
    "raw, expected",
    [
        (SUB, f"/subscriptions/{SUB}"),
        (f"subscriptions/{SUB}/", f"/subscriptions/{SUB}"),
        (f"https://management.azure.com/subscriptions/{SUB}/resourceGroups/rg1",
         f"/subscriptions/{SUB}/resourceGroups/rg1"),
        ("/providers/Microsoft.Billing/billingAccounts/1234:5678",
         "/providers/Microsoft.Billing/billingAccounts/1234:5678"),
    ],
)
def test_scope_is_normalized_to_an_arm_path(raw, expected):
    assert normalize_scope(raw) == expected


def test_empty_scope_is_rejected():
    with pytest.raises(ValueError):
        normalize_scope("  ")


# ---------- body construction ---------- #


def test_default_query_is_last_30_days_total_actual_cost():
    c, _ = _client([])
    body = c.build_body({})
    assert body["type"] == "ActualCost"
    assert body["timeframe"] == "Custom"
    assert body["timePeriod"]["from"].startswith("2026-09-15")
    assert body["timePeriod"]["to"].startswith("2026-10-15")
    assert "granularity" not in body["dataset"]
    assert list(body["dataset"]["aggregation"].values())[0]["function"] == "Sum"


@pytest.mark.parametrize(
    "start, expected_from",
    [
        ("start_of_month", "2026-10-01"),
        ("start_of_last_month", "2026-09-01"),
        ("-3m", "2026-07-01"),
        ("-2w", "2026-10-01"),
        ("2025-01-31", "2025-01-31"),
    ],
)
def test_relative_and_absolute_start_dates_resolve(start, expected_from):
    c, _ = _client([])
    assert c.build_body({"start": start})["timePeriod"]["from"].startswith(expected_from)


def test_named_timeframe_is_passed_through_without_time_period():
    c, _ = _client([])
    body = c.build_body({"timeframe": "thelastmonth"})
    assert body["timeframe"] == "TheLastMonth"
    assert "timePeriod" not in body


def test_group_by_maps_dimensions_and_tags():
    c, _ = _client([])
    body = c.build_body({"group_by": ["ServiceName", "tag:Environment"]})
    assert body["dataset"]["grouping"] == [
        {"type": "Dimension", "name": "ServiceName"},
        {"type": "TagKey", "name": "Environment"},
    ]


def test_more_than_two_groupings_is_rejected_before_calling_azure():
    c, session = _client([])
    with pytest.raises(ValueError):
        c.execute_query({"group_by": ["ServiceName", "ResourceGroupName", "ResourceLocation"]})
    assert session.calls == []


def test_flat_filter_with_one_key_is_a_single_clause():
    # The API rejects an `and` with fewer than two members.
    c, _ = _client([])
    flt = c.build_body({"filter": {"ResourceGroupName": "rg-prod"}})["dataset"]["filter"]
    assert flt == {"dimensions": {"name": "ResourceGroupName", "operator": "In", "values": ["rg-prod"]}}


def test_flat_filter_with_several_keys_is_anded_and_tags_are_recognized():
    c, _ = _client([])
    flt = c.build_body({"filter": {"ResourceGroupName": ["a", "b"], "tag:env": ["prod"]}})["dataset"]["filter"]
    assert len(flt["and"]) == 2
    kinds = {next(iter(clause)) for clause in flt["and"]}
    assert kinds == {"dimensions", "tags"}


def test_native_filter_grammar_is_passed_through():
    native = {"or": [{"dimensions": {"name": "ResourceLocation", "operator": "In", "values": ["East US"]}},
                     {"tags": {"name": "env", "operator": "In", "values": ["prod"]}}]}
    c, _ = _client([])
    assert c.build_body({"filter": native})["dataset"]["filter"] == native


def test_amortized_cost_type_and_monthly_granularity():
    c, _ = _client([])
    body = c.build_body({"type": "amortizedcost", "granularity": "monthly"})
    assert body["type"] == "AmortizedCost"
    assert body["dataset"]["granularity"] == "Monthly"


def test_connection_default_cost_type_is_used():
    c, _ = _client([], cost_type="AmortizedCost")
    assert c.build_body({})["type"] == "AmortizedCost"


@pytest.mark.parametrize("bad", [{"metric": "Profit"}, {"type": "Nope"}, {"granularity": "Hourly"},
                                 {"start": "last tuesday"}, {"start": "2026-10-10", "end": "2026-10-01"}])
def test_invalid_envelope_values_raise_value_error(bad):
    c, _ = _client([])
    with pytest.raises(ValueError):
        c.build_body(bad)


def test_sql_string_is_rejected_with_guidance():
    c, _ = _client([])
    with pytest.raises(ValueError):
        c.execute_query("SELECT * FROM costs")


# ---------- execution + shaping ---------- #


def test_grouped_daily_query_returns_frame_with_parsed_dates():
    payload = _query_payload(
        [("Cost", "Number"), ("UsageDate", "Number"), ("ServiceName", "String"), ("Currency", "String")],
        [[12.5, 20261001, "Virtual Machines", "USD"], [3.25, 20261002, "Storage", "USD"]],
    )
    c, session = _client([_Resp(200, payload)])
    df = c.execute_query({"start": "start_of_month", "granularity": "Daily", "group_by": ["ServiceName"]})

    assert list(df.columns) == ["Cost", "UsageDate", "ServiceName", "Currency"]
    assert len(df) == 2
    assert df["UsageDate"].iloc[0] == dt.date(2026, 10, 1)
    call = session.calls[0]
    assert call["method"] == "POST"
    assert f"/subscriptions/{SUB}/providers/Microsoft.CostManagement/query" in call["url"]
    assert call["headers"]["Authorization"] == "Bearer fake-token"


def test_next_link_pages_are_followed_with_the_same_body():
    cols = [("Cost", "Number"), ("ResourceId", "String"), ("Currency", "String")]
    next_url = "https://management.azure.com/next?$skiptoken=abc"
    c, session = _client([
        _Resp(200, _query_payload(cols, [[1.0, "r1", "USD"]], next_link=next_url)),
        _Resp(200, _query_payload(cols, [[2.0, "r2", "USD"]])),
    ])
    df = c.execute_query({"group_by": ["ResourceId"]})
    assert df["ResourceId"].tolist() == ["r1", "r2"]
    assert session.calls[1]["url"] == next_url
    assert session.calls[1]["json"] == session.calls[0]["json"]


def test_limit_caps_rows_and_stops_paging():
    cols = [("Cost", "Number"), ("ResourceId", "String")]
    c, session = _client([
        _Resp(200, _query_payload(cols, [[1.0, "a"], [2.0, "b"], [3.0, "c"]], next_link="https://x/next")),
    ])
    df = c.execute_query({"group_by": ["ResourceId"], "limit": 2})
    assert len(df) == 2
    assert len(session.calls) == 1


def test_no_content_returns_empty_frame():
    c, _ = _client([_Resp(204)])
    df = c.execute_query({})
    assert isinstance(df, pd.DataFrame) and df.empty


def test_source_named_cost_column_is_exposed_under_requested_metric():
    # Documented responses name the column after the source (PreTaxCost) even
    # when another alias was requested; code written against `Cost` must work.
    payload = _query_payload([("PreTaxCost", "Number"), ("Currency", "String")], [[10.0, "EUR"]])
    c, _ = _client([_Resp(200, payload)])
    df = c.execute_query({})
    assert "Cost" in df.columns and df["Cost"].iloc[0] == 10.0


def test_default_metric_rejected_falls_back_to_pretaxcost():
    bad = _Resp(400, {"error": {"code": "BadRequest", "message": "Invalid aggregation column Cost"}})
    ok = _Resp(200, _query_payload([("PreTaxCost", "Number"), ("Currency", "String")], [[5.0, "USD"]]))
    c, session = _client([bad, ok])
    df = c.execute_query({})
    assert df["Cost"].iloc[0] == 5.0
    assert "PreTaxCost" in session.calls[1]["json"]["dataset"]["aggregation"]


def test_explicit_metric_rejected_is_not_silently_swapped():
    bad = _Resp(400, {"error": {"code": "BadRequest", "message": "Invalid"}})
    c, session = _client([bad])
    with pytest.raises(AzureCostError) as ei:
        c.execute_query({"metric": "CostUSD"})
    assert ei.value.status == 400
    assert len(session.calls) == 1


# ---------- throttling ---------- #


def test_throttled_request_waits_for_longest_cost_management_header_then_retries():
    sleeps = []
    throttled = _Resp(429, {"error": {"code": "429", "message": "Too many requests"}}, headers={
        "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after": "7",
        "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after": "3",
    })
    ok = _Resp(200, _query_payload([("Cost", "Number")], [[1.0]]))
    c, session = _client([throttled, ok], sleeps=sleeps)
    df = c.execute_query({})
    assert len(df) == 1
    assert sleeps == [7.0]
    assert len(session.calls) == 2


def test_throttling_beyond_the_wait_budget_fails_with_quota_error():
    throttled = _Resp(429, {"error": {"code": "429", "message": "Too many requests"}},
                      headers={"x-ms-ratelimit-microsoft.consumption-retry-after": "3600"})
    c, session = _client([throttled])
    with pytest.raises(AzureCostError) as ei:
        c.execute_query({})
    assert ei.value.status == 429
    assert len(session.calls) == 1


def test_persistent_throttling_gives_up_after_bounded_retries():
    sleeps = []

    def throttled(*_):
        return _Resp(429, {"error": {"code": "429", "message": "x"}}, headers={"Retry-After": "1"})

    c, session = _client([throttled] * 10, sleeps=sleeps)
    with pytest.raises(AzureCostError):
        c.execute_query({})
    assert 1 < len(session.calls) <= 5


# ---------- other tables ---------- #


def test_forecast_uses_forecast_endpoint_with_future_window():
    payload = _query_payload(
        [("Cost", "Number"), ("UsageDate", "Number"), ("CostStatus", "String"), ("Currency", "String")],
        [[2.0, 20261020, "Forecast", "USD"]],
    )
    c, session = _client([_Resp(200, payload)])
    df = c.execute_query({"table": "forecast"})
    call = session.calls[0]
    assert "/Microsoft.CostManagement/forecast" in call["url"]
    assert call["json"]["timeframe"] == "Custom"
    assert call["json"]["timePeriod"]["to"].startswith("2026-10-31")
    assert call["json"]["dataset"]["granularity"] == "Daily"
    assert df["CostStatus"].iloc[0] == "Forecast"


def test_dimension_values_lists_values_per_dimension():
    payload = {"value": [
        {"properties": {"category": "ResourceGroupName", "data": ["rg-a", "rg-b"]}},
    ]}
    c, session = _client([_Resp(200, payload)])
    df = c.execute_query({"table": "dimension_values", "dimension": "ResourceGroupName"})
    assert df["value"].tolist() == ["rg-a", "rg-b"]
    assert session.calls[0]["method"] == "GET"
    assert "ResourceGroupName" in session.calls[0]["url"]


def test_unknown_table_is_rejected():
    c, _ = _client([])
    with pytest.raises(ValueError):
        c.execute_query({"table": "invoices"})


# ---------- discovery ---------- #


def test_schemas_expose_cost_forecast_and_values_tables_with_discovered_dimensions():
    payload = {"value": [
        {"properties": {"category": "ResourceGroupName", "groupingEnabled": True, "filterEnabled": True}},
        {"properties": {"category": "ChargeType", "groupingEnabled": False, "filterEnabled": True}},
        {"properties": {"category": "Hidden", "groupingEnabled": False, "filterEnabled": False}},
    ]}
    c, _ = _client([_Resp(200, payload)])
    tables = {t.name: t for t in c.get_schemas()}
    assert set(tables) == {"costs", "forecast", "dimension_values"}
    cost_cols = {col.name for col in tables["costs"].columns}
    assert {"Cost", "Currency", "UsageDate", "ResourceGroupName", "ChargeType"} <= cost_cols
    assert "Hidden" not in cost_cols


def test_schemas_fall_back_to_common_dimensions_when_listing_fails():
    c, _ = _client([_Resp(403, {"error": {"code": "AuthorizationFailed", "message": "no"}})])
    cost_cols = {col.name for col in c.get_schema("costs").columns}
    assert {"ServiceName", "ResourceGroupName", "MeterCategory"} <= cost_cols


# ---------- connection test ---------- #


def test_connection_test_reports_month_to_date_spend():
    payload = _query_payload([("Cost", "Number"), ("Currency", "String")], [[123.4, "USD"]])
    c, session = _client([_Resp(200, payload)])
    result = c.test_connection()
    assert result["success"] is True
    assert session.calls[0]["json"]["timeframe"] == "MonthToDate"


def test_connection_test_surfaces_permission_failure():
    denied = _Resp(403, {"error": {"code": "RBACAccessDenied", "message": "The client does not have authorization"}})
    c, _ = _client([denied])
    result = c.test_connection()
    assert result["success"] is False
    assert "Cost Management Reader" in result["message"]


def test_connection_test_surfaces_token_failure():
    class _BadCred:
        def get_token(self, *a):
            raise RuntimeError("AADSTS7000215: Invalid client secret")

    c, session = _client([])
    c._credential = _BadCred()
    result = c.test_connection()
    assert result["success"] is False
    assert session.calls == []


# ---------- registry ---------- #


def test_registry_resolves_client_and_exposes_both_auth_variants():
    from app.schemas.data_source_registry import REGISTRY, resolve_client_class

    assert resolve_client_class("azure_cost") is AzureCostClient
    entry = REGISTRY["azure_cost"]
    assert set(entry.credentials_auth.by_auth) == {"service_principal", "managed_identity"}
    assert "scope" in entry.config_schema.model_json_schema()["properties"]

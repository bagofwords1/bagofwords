"""Opt-in live verification of the Azure Cost Management connector.

Runs the real AzureCostClient against a real Azure scope and records the facts
the unit tests can only assume (see docs/feedback-loops/azure-cost-connector.md):

  1. test_connection() works with the given identity/role
  2. the Dimensions API returns a real list for this scope (vs our fallback)
  3. which cost column the Query API returns (Cost vs PreTaxCost) per account type
  4. grouping by dimension names we tell the agent to use (ServiceName,
     ResourceGroupName, ResourceGroup, MeterCategory) and by a tag key
  5. UsageDate / BillingMonth shapes for Daily / Monthly granularity
  6. the Forecast API and dimension values
  7. whether a >12-month custom range is rejected
  8. the throttling / QPU headers the service actually sends

Read-only: every call is a POST query/forecast or GET dimensions. Roughly 30
QPU in total (the >12-month probe alone costs ~13); calls are spaced out to
stay under the 12 QPU / 10 s tenant quota.

Usage (from backend/):
    AZURE_COST_SCOPE=/subscriptions/<id> \\
    AZURE_TENANT_ID=... AZURE_CLIENT_ID=... AZURE_CLIENT_SECRET=... \\
    uv run python tests/integrations/verify_azure_cost.py [--tag-key Environment] [--out report.json]

Omit the three AZURE_* secrets to use DefaultAzureCredential (az login /
managed identity).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

PAUSE_SECONDS = int(os.environ.get("AZURE_COST_PAUSE_SECONDS", "12"))


class RecordingSession:
    """requests.Session wrapper that keeps status + rate-limit headers per call."""

    def __init__(self):
        import requests

        self._s = requests.Session()
        self.log = []

    def request(self, method, url, **kwargs):
        resp = self._s.request(method, url, **kwargs)
        headers = {
            k: v for k, v in resp.headers.items()
            if k.lower().startswith("x-ms-ratelimit") or k.lower() == "retry-after"
        }
        self.log.append({
            "method": method,
            "url": url.split("?")[0],
            "status": resp.status_code,
            "ratelimit_headers": headers,
        })
        return resp


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag-key", default=None, help="A tag key that exists on resources in the scope.")
    parser.add_argument("--out", default="azure_cost_verification.json")
    args = parser.parse_args()

    scope = os.environ.get("AZURE_COST_SCOPE")
    if not scope:
        raise SystemExit("Set AZURE_COST_SCOPE (e.g. /subscriptions/<id>).")

    from app.data_sources.clients.azure_cost_client import AzureCostClient

    session = RecordingSession()
    client = AzureCostClient(
        scope=scope,
        tenant_id=os.environ.get("AZURE_TENANT_ID"),
        client_id=os.environ.get("AZURE_CLIENT_ID"),
        client_secret=os.environ.get("AZURE_CLIENT_SECRET"),
        _session=session,
    )

    report: dict = {"scope": client.scope, "checks": {}}

    def check(name, fn):
        print(f"\n== {name}")
        try:
            result = fn()
            report["checks"][name] = {"ok": True, **(result or {})}
            print(json.dumps(result, indent=2, default=str)[:2000])
        except Exception as e:
            report["checks"][name] = {"ok": False, "error": str(e), "trace": traceback.format_exc()[-1500:]}
            print(f"FAILED: {e}")
        time.sleep(PAUSE_SECONDS)

    def frame_summary(df):
        return {
            "columns": list(df.columns),
            "dtypes": {c: str(t) for c, t in df.dtypes.items()},
            "rows": len(df),
            "head": df.head(5).to_dict(orient="records"),
        }

    def connection():
        result = client.test_connection()
        if not result.get("success"):
            raise RuntimeError(result.get("message"))
        return result
    check("1_test_connection", connection)

    def dims():
        client._dimensions_cache = None
        before = len(session.log)
        tables = client.get_schemas()
        listed = session.log[before:]
        cols = [c.name for c in tables[0].columns]
        statuses = [c["status"] for c in listed]
        if not statuses or statuses[-1] != 200:
            # get_schemas() falls back to a built-in dimension list on any
            # failure, so a "pass" here must mean the Dimensions API answered.
            raise RuntimeError(f"Dimensions API did not answer 200 (statuses: {statuses}); columns are the built-in fallback")
        return {"dimensions_api_status": statuses, "costs_columns": cols}
    check("2_dimensions_discovery", dims)

    def raw_cost_column():
        body = client.build_body({"timeframe": "MonthToDate"})
        payload = client._request("POST", client._url("query"), body)
        cols = [c["name"] for c in ((payload or {}).get("properties") or {}).get("columns", [])]
        result = {"requested_aggregation": body["dataset"]["aggregation"], "returned_columns": cols}
        body["dataset"]["aggregation"] = {"PreTaxCost": {"name": "PreTaxCost", "function": "Sum"}}
        time.sleep(PAUSE_SECONDS)
        try:
            payload = client._request("POST", client._url("query"), body)
            result["pretaxcost_returned_columns"] = [
                c["name"] for c in ((payload or {}).get("properties") or {}).get("columns", [])
            ]
        except Exception as e:
            result["pretaxcost_error"] = str(e)
        return result
    check("3_cost_column_name", raw_cost_column)

    for dim in ("ServiceName", "ResourceGroupName", "ResourceGroup", "MeterCategory"):
        check(f"4_group_by_{dim}", lambda d=dim: frame_summary(
            client.execute_query({"timeframe": "MonthToDate", "group_by": [d], "limit": 20})))
    if args.tag_key:
        check("4_group_by_tag", lambda: frame_summary(
            client.execute_query({"timeframe": "MonthToDate", "group_by": [f"tag:{args.tag_key}"], "limit": 20})))

    check("5_daily", lambda: frame_summary(
        client.execute_query({"start": "-7d", "granularity": "Daily", "group_by": ["ServiceName"], "limit": 50})))
    check("5_monthly", lambda: frame_summary(
        client.execute_query({"start": "-2m", "granularity": "Monthly"})))
    check("5_amortized", lambda: frame_summary(
        client.execute_query({"type": "AmortizedCost", "timeframe": "TheLastMonth", "group_by": ["ChargeType"]})))

    check("6_forecast", lambda: frame_summary(client.execute_query({"table": "forecast"})))
    check("6_dimension_values", lambda: frame_summary(
        client.execute_query({"table": "dimension_values", "dimension": "ResourceGroupName", "limit": 20})))

    check("7_range_over_12_months", lambda: frame_summary(
        client.execute_query({"start": "-13m", "granularity": "Monthly"})))

    report["http_log"] = session.log
    Path(args.out).write_text(json.dumps(report, indent=2, default=str))
    failed = [k for k, v in report["checks"].items() if not v["ok"]]
    print(f"\nWrote {args.out}. {len(report['checks']) - len(failed)} ok, {len(failed)} failed: {failed}")
    # 7 is expected to fail if Azure enforces the 1-year limit, and
    # ResourceGroup vs ResourceGroupName may legitimately differ — those are
    # findings, not regressions, so don't fail the process on them.
    hard = [k for k in failed if not k.startswith(("7_", "4_group_by_ResourceGroup"))]
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Answer the open questions in the `datadog` connector plan against a real org.

Read-only. Needs an API key + application key (read scopes) in the env:

  DD_API_KEY=... DD_APP_KEY=... DD_SITE=datadoghq.com python tools/datadog/spike_ddsql.py

Spikes:
  1. Can the DDSQL table catalog be listed programmatically
     (information_schema / pg_catalog)? Decides how `get_schemas` works.
  2. DDSQL execution behavior: submit → poll latency, poll count, response
     shape (column-major `columns[{name,type,values}]`), row_limit handling.
  3. Do the table functions (`dd.logs`, `dd.metrics_timeseries`,
     `dd.metrics_scalar`) answer over the data `seed_datadog.py` writes?
  4. Catalog side-APIs the connector would use: metric list, dashboards,
     monitors, log indexes — reachable with these key scopes?

Prints a markdown report; paste it into docs/design/datadog-connector.md.
Raw responses land in ./ddsql_spike_out/ for inspection (never committed).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

SITE = os.environ.get("DD_SITE", "datadoghq.com")
BASE = f"https://api.{SITE}"
OUT = os.path.join(os.getcwd(), "ddsql_spike_out")
HEADERS = {
    "DD-API-KEY": os.environ.get("DD_API_KEY", ""),
    "DD-APPLICATION-KEY": os.environ.get("DD_APP_KEY", ""),
    "Content-Type": "application/json",
}


def call(method: str, path: str, body=None):
    req = urllib.request.Request(
        BASE + path, method=method, headers=HEADERS,
        data=json.dumps(body).encode() if body is not None else None,
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}"), time.time() - t0
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw), time.time() - t0
        except ValueError:
            return e.code, {"raw": raw[:1000]}, time.time() - t0


def dump(name: str, payload) -> None:
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"{name}.json"), "w") as f:
        json.dump(payload, f, indent=2, default=str)


def ddsql(name: str, query: str, hours: float = 6, row_limit: int = 100, max_wait: float = 90):
    now_ms = int(time.time() * 1000)
    body = {"data": {"type": "ddsql_query_request", "attributes": {
        "query": query, "row_limit": row_limit,
        "time": {"from_timestamp": now_ms - int(hours * 3600_000), "to_timestamp": now_ms},
    }}}
    status, resp, elapsed = call("POST", "/api/v2/ddsql/query/tabular", body)
    polls = 0
    t0 = time.time()
    while status == 200 and resp.get("data", {}).get("attributes", {}).get("state") == "running":
        if time.time() - t0 > max_wait:
            break
        time.sleep(1)
        polls += 1
        qid = resp["data"]["attributes"]["query_id"]
        status, resp, _ = call("POST", "/api/v2/ddsql/query/tabular/fetch",
                               {"data": {"type": "ddsql_query_fetch_request", "attributes": {"query_id": qid}}})
    total = elapsed + (time.time() - t0)
    dump(name, resp)
    attrs = resp.get("data", {}).get("attributes", {})
    cols = attrs.get("columns") or []
    rows = len(cols[0]["values"]) if cols else 0
    err = resp.get("errors")
    return {
        "name": name, "http": status, "state": attrs.get("state"), "polls": polls,
        "secs": round(total, 1), "rows": rows,
        "columns": [f"{c['name']}:{c['type']}" for c in cols][:12],
        "warnings": attrs.get("warnings"), "errors": err,
    }


SPIKE_QUERIES = [
    # 1. catalog introspection
    ("info_schema_tables",
     "SELECT table_schema, table_name FROM information_schema.tables LIMIT 500"),
    ("info_schema_columns",
     "SELECT table_name, column_name, data_type FROM information_schema.columns "
     "WHERE table_name = 'hosts' LIMIT 200"),
    ("pg_catalog_tables", "SELECT schemaname, tablename FROM pg_catalog.pg_tables LIMIT 500"),
    # 2. trivial + resource table
    ("select_one", "SELECT 1 AS one"),
    ("dd_hosts", "SELECT * FROM dd.hosts LIMIT 5"),
    # 3. table functions over seeded data
    ("logs_errors_by_service",
     "SELECT service, count(*) AS errors FROM dd.logs("
     "filter => 'env:bow-demo status:error', columns => ARRAY['service']"
     ") AS (service VARCHAR) GROUP BY service ORDER BY errors DESC"),
    ("logs_attr_columns",
     "SELECT * FROM dd.logs(filter => 'env:bow-demo', "
     "columns => ARRAY['timestamp','service','@error.kind','@duration_ms']"
     ") AS (ts TIMESTAMP, service VARCHAR, error_kind VARCHAR, duration_ms DOUBLE) LIMIT 20"),
    ("metrics_timeseries",
     "SELECT * FROM dd.metrics_timeseries("
     "'avg:bow.demo.request.latency.p95{env:bow-demo} by {service}') LIMIT 50"),
    ("metrics_scalar",
     "SELECT * FROM dd.metrics_scalar("
     "'sum:bow.demo.request.errors{env:bow-demo} by {service}', 'sum')"),
    # row_limit behavior
    ("row_limit_probe",
     "SELECT * FROM dd.logs(filter => 'env:bow-demo', columns => ARRAY['service']) "
     "AS (service VARCHAR)"),
]

SIDE_APIS = [
    ("metrics_list", "GET", "/api/v2/metrics?filter[tags]=env:bow-demo&page[size]=100"),
    ("metrics_search_v1", "GET", "/api/v1/search?q=metrics:bow.demo"),
    ("dashboards", "GET", "/api/v1/dashboard?count=20"),
    ("monitors", "GET", "/api/v1/monitor?page_size=20"),
    ("log_indexes", "GET", "/api/v1/logs/config/indexes"),
    ("events", "GET", "/api/v2/events?filter[query]=source:bow-seed&page[limit]=10"),
]


def main() -> None:
    if not HEADERS["DD-API-KEY"] or not HEADERS["DD-APPLICATION-KEY"]:
        sys.exit("DD_API_KEY and DD_APP_KEY are required")
    status, resp, _ = call("GET", "/api/v1/validate")
    print(f"# Datadog connector spike — site `{SITE}`\n\nkey validate: HTTP {status} {resp}\n")

    print("## DDSQL\n\n| query | http | state | polls | secs | rows | columns / error |\n|---|---|---|---|---|---|---|")
    for name, q in SPIKE_QUERIES:
        r = ddsql(name, q, row_limit=10000 if name == "row_limit_probe" else 100)
        detail = r["errors"] or r["columns"]
        if r["warnings"]:
            detail = f"{detail} warnings={r['warnings']}"
        print(f"| {name} | {r['http']} | {r['state']} | {r['polls']} | {r['secs']} | {r['rows']} | "
              f"{json.dumps(detail)[:300]} |")

    print("\n## Side APIs\n\n| api | http | secs | size |\n|---|---|---|---|")
    for name, method, path in SIDE_APIS:
        status, resp, secs = call(method, path)
        dump(name, resp)
        data = resp.get("data", resp.get("dashboards", resp.get("indexes", resp)))
        size = len(data) if isinstance(data, list) else (len(data.get("metrics", [])) if isinstance(data, dict) else "?")
        if status >= 400:
            size = json.dumps(resp)[:200]
        print(f"| {name} | {status} | {round(secs, 1)} | {size} |")
    print(f"\nRaw responses: {OUT}")


if __name__ == "__main__":
    main()

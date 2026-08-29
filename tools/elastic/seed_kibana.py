#!/usr/bin/env python3
"""Seed Kibana with the RCA knowledge layer the connector's dashboard support
catalogs, plus recent incident data to investigate.

Creates:
  - data views for `frontend-*` / `billing-*` / `backend-*`
  - dashboard "Checkout Health": a by-reference Lens panel (formBased aggs),
    a by-value Lens ES|QL panel, a Discover saved-search panel, and a legacy
    visualization panel — one of each dialect the connector parses
  - dashboard "Backend Errors" (single by-ref Lens)
  - saved search "Failed checkout requests"
  - Elastic's own Flights sample data set (real Elastic-authored dashboard —
    a parser stress test; skipped gracefully if the API is unavailable)
  - ~3k recent log events (last 48h) with a billing-service error spike in
    the last 4h, so "checkout errors spiked, why?" has a real answer

Also sets the kibana_system password on Elasticsearch (idempotent), which the
docker-compose Kibana waits on.

Usage:  python seed_kibana.py   (ES on :9200, Kibana on :5601)
"""
import base64
import json
import random
import time
import urllib.error
import urllib.request

ES = "http://127.0.0.1:9200"
KB = "http://127.0.0.1:5601"
AUTH = "elastic:elastic_pwd"
random.seed(20260828)

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(base, method, path, body=None, raw=False, ok404=False):
    h = {"Content-Type": "application/json", "kbn-xsrf": "true",
         "Authorization": "Basic " + base64.b64encode(AUTH.encode()).decode()}
    data = None
    if body is not None:
        data = (body if isinstance(body, bytes) else body.encode()) if raw \
            else json.dumps(body).encode()
    r = urllib.request.Request(f"{base}{path}", data=data, headers=h, method=method)
    try:
        with _OPENER.open(r, timeout=120) as resp:
            return json.loads(resp.read() or "{}")
    except urllib.error.HTTPError as e:
        if ok404 and e.code == 404:
            return None
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read()[:400]}")


def ensure_kibana_system_password():
    req(ES, "POST", "/_security/user/kibana_system/_password",
        {"password": "kibana_pwd"})
    print("kibana_system password ensured")


def wait_kibana():
    for _ in range(120):
        try:
            st = req(KB, "GET", "/api/status")
            if ((st.get("status") or {}).get("overall") or {}).get("level") == "available":
                print("kibana available")
                return
        except Exception:
            pass
        time.sleep(5)
    raise RuntimeError("Kibana did not become available")


# ---- recent incident data (billing spike in the last 4h) ----

NOW = time.time()
HOSTS = [f"host-{i:02d}" for i in range(1, 7)]


def _doc(ts, service, level, status, msg, latency):
    return {"@timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
            "service": service, "host": random.choice(HOSTS), "level": level,
            "syslog_severity": {"info": "info", "warn": "notice",
                                "error": "err", "fatal": "crit"}[level],
            "status": status, "internal_error": level in ("error", "fatal"),
            "err": msg if level in ("error", "fatal") else "",
            "code_string": "Internal" if level in ("error", "fatal") else "OK",
            "latency_ms": latency, "user_id": f"u{random.randint(1, 300)}",
            "message": msg}


def seed_recent():
    docs = []
    for _ in range(2200):  # baseline, last 48h, mostly healthy
        ts = NOW - random.uniform(0, 48 * 3600)
        svc = random.choice(["frontend", "backend", "billing"])
        if random.random() < 0.06:
            docs.append((svc, _doc(ts, svc, "error", random.choice([500, 502]),
                                   "internal error while processing",
                                   random.uniform(100, 900))))
        else:
            docs.append((svc, _doc(ts, svc, "info", 200,
                                   random.choice(["checkout completed", "request handled",
                                                  "cache hit", "healthcheck ok"]),
                                   random.uniform(5, 250))))
    for _ in range(500):  # the incident: billing timeouts, last 4h
        ts = NOW - random.uniform(0, 4 * 3600)
        docs.append(("billing", _doc(ts, "billing", "error",
                                     random.choice([502, 503, 503]),
                                     random.choice(["timeout talking to upstream payment gateway",
                                                    "connection refused",
                                                    "circuit breaker open"]),
                                     random.uniform(1800, 3000))))
    for _ in range(260):  # correlated frontend checkout failures, last 4h
        ts = NOW - random.uniform(0, 4 * 3600)
        docs.append(("frontend", _doc(ts, "frontend", "error", 500,
                                      "checkout failed: billing unavailable",
                                      random.uniform(900, 2500))))
    buf = []
    for svc, d in docs:
        day = d["@timestamp"][:10].replace("-", ".")
        buf.append(json.dumps({"index": {"_index": f"{svc}-{day}"}}))
        buf.append(json.dumps(d))
    req(ES, "POST", "/_bulk", "\n".join(buf) + "\n", raw=True)
    req(ES, "POST", "/_refresh")
    print(f"seeded {len(docs)} recent events (billing spike in last 4h)")


# ---- saved objects (deterministic ids so re-runs overwrite) ----

DV_FRONTEND = "bow-dv-frontend"
DV_BILLING = "bow-dv-billing"
DV_BACKEND = "bow-dv-backend"
LENS_5XX = "bow-lens-5xx-by-service"
LENS_BACKEND = "bow-lens-backend-errors"
VIZ_HOSTS = "bow-viz-errors-by-host"
SEARCH_CHECKOUT = "bow-search-failed-checkout"
DASH_CHECKOUT = "bow-dash-checkout-health"
DASH_BACKEND = "bow-dash-backend-errors"

ESQL_QUERY = ("FROM frontend-*,billing-* | WHERE status >= 500 "
              "| STATS errors = COUNT(*) BY service | SORT errors DESC")


def _data_view(id_, title, name):
    return {"type": "index-pattern", "id": id_,
            "attributes": {"title": title, "name": name,
                           "timeFieldName": "@timestamp", "fields": "[]"},
            "references": []}


def _lens_formbased(id_, title, dv_ref, field="service"):
    cols = {
        "c_time": {"label": "@timestamp", "dataType": "date",
                   "operationType": "date_histogram", "sourceField": "@timestamp",
                   "isBucketed": True, "params": {"interval": "auto"}},
        "c_count": {"label": "Count of records", "dataType": "number",
                    "operationType": "count", "sourceField": "___records___",
                    "isBucketed": False},
        "c_split": {"label": field, "dataType": "string", "operationType": "terms",
                    "sourceField": field, "isBucketed": True,
                    "params": {"size": 10, "orderDirection": "desc",
                               "orderBy": {"type": "column", "columnId": "c_count"}}},
    }
    return {"type": "lens", "id": id_, "attributes": {
        "title": title, "visualizationType": "lnsXY",
        "state": {
            "query": {"query": "status >= 500", "language": "kuery"},
            "filters": [],
            "datasourceStates": {"formBased": {"layers": {"layer1": {
                "columns": cols, "columnOrder": ["c_split", "c_time", "c_count"],
                "incompleteColumns": {}}}}},
            "visualization": {"preferredSeriesType": "bar_stacked",
                              "legend": {"isVisible": True, "position": "right"},
                              "layers": [{"layerId": "layer1", "layerType": "data",
                                          "seriesType": "bar_stacked",
                                          "accessors": ["c_count"],
                                          "xAccessor": "c_time",
                                          "splitAccessor": "c_split"}]},
        }},
        "references": [{"type": "index-pattern", "id": dv_ref,
                        "name": "indexpattern-datasource-layer-layer1"}]}


def _lens_esql_attrs():
    """By-VALUE Lens (embedded in the dashboard panel), ES|QL datasource."""
    return {
        "title": "Error count by service (ES|QL)",
        "visualizationType": "lnsDatatable",
        "state": {
            "query": {"esql": ESQL_QUERY}, "filters": [],
            "datasourceStates": {"textBased": {"layers": {"l1": {
                "query": {"esql": ESQL_QUERY},
                "columns": [{"columnId": "service", "fieldName": "service"},
                            {"columnId": "errors", "fieldName": "errors"}],
                "index": "frontend-*"}}}},
            "visualization": {"layerId": "l1", "layerType": "data",
                              "columns": [{"columnId": "service"},
                                          {"columnId": "errors"}]},
        },
        "references": [],
    }


def _legacy_viz():
    vis_state = {"title": "Errors by host (legacy)", "type": "table",
                 "aggs": [{"id": "1", "enabled": True, "type": "count",
                           "schema": "metric", "params": {}},
                          {"id": "2", "enabled": True, "type": "terms",
                           "schema": "bucket",
                           "params": {"field": "host", "size": 10,
                                      "order": "desc", "orderBy": "1"}}],
                 "params": {}}
    sso = {"query": {"query": "level:error OR level:fatal", "language": "lucene"},
           "filter": [],
           "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index"}
    return {"type": "visualization", "id": VIZ_HOSTS, "attributes": {
        "title": "Errors by host (legacy)",
        "visState": json.dumps(vis_state), "uiStateJSON": "{}",
        "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps(sso)}},
        "references": [{"name": "kibanaSavedObjectMeta.searchSourceJSON.index",
                        "type": "index-pattern", "id": DV_BILLING}]}


def _saved_search():
    sso = {"query": {"query": "status:[500 TO 599] AND message:checkout*",
                     "language": "lucene"},
           "filter": [],
           "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index"}
    return {"type": "search", "id": SEARCH_CHECKOUT, "attributes": {
        "title": "Failed checkout requests",
        "description": "Investigation query for checkout incidents.",
        "columns": ["service", "host", "status", "message"],
        "sort": [["@timestamp", "desc"]],
        "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps(sso)}},
        "references": [{"name": "kibanaSavedObjectMeta.searchSourceJSON.index",
                        "type": "index-pattern", "id": DV_FRONTEND}]}


def _dashboard_checkout():
    panels = [
        {"version": "8.15.3", "type": "lens",
         "gridData": {"x": 0, "y": 0, "w": 24, "h": 15, "i": "p1"},
         "panelIndex": "p1", "embeddableConfig": {"enhancements": {}},
         "title": "5xx by service", "panelRefName": "panel_p1"},
        {"version": "8.15.3", "type": "lens",
         "gridData": {"x": 24, "y": 0, "w": 24, "h": 15, "i": "p2"},
         "panelIndex": "p2",
         "embeddableConfig": {"attributes": _lens_esql_attrs(), "enhancements": {}},
         "title": "Error count by service (ES|QL)"},
        {"version": "8.15.3", "type": "search",
         "gridData": {"x": 0, "y": 15, "w": 24, "h": 15, "i": "p3"},
         "panelIndex": "p3", "embeddableConfig": {},
         "title": "Failed checkout requests", "panelRefName": "panel_p3"},
        {"version": "8.15.3", "type": "visualization",
         "gridData": {"x": 24, "y": 15, "w": 24, "h": 15, "i": "p4"},
         "panelIndex": "p4", "embeddableConfig": {},
         "title": "Errors by host (legacy)", "panelRefName": "panel_p4"},
    ]
    return {"type": "dashboard", "id": DASH_CHECKOUT, "attributes": {
        "title": "Checkout Health",
        "description": "Panels the payments team uses for checkout incidents.",
        "panelsJSON": json.dumps(panels),
        "optionsJSON": json.dumps({"useMargins": True}),
        "timeRestore": False,
        "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps(
            {"query": {"query": "", "language": "kuery"}, "filter": []})}},
        "references": [
            {"name": "p1:panel_p1", "type": "lens", "id": LENS_5XX},
            {"name": "p3:panel_p3", "type": "search", "id": SEARCH_CHECKOUT},
            {"name": "p4:panel_p4", "type": "visualization", "id": VIZ_HOSTS},
        ]}


def _dashboard_backend():
    panels = [
        {"version": "8.15.3", "type": "lens",
         "gridData": {"x": 0, "y": 0, "w": 48, "h": 15, "i": "b1"},
         "panelIndex": "b1", "embeddableConfig": {"enhancements": {}},
         "title": "Backend errors over time", "panelRefName": "panel_b1"},
    ]
    return {"type": "dashboard", "id": DASH_BACKEND, "attributes": {
        "title": "Backend Errors",
        "description": "SRE on-call view of backend service errors.",
        "panelsJSON": json.dumps(panels),
        "optionsJSON": json.dumps({"useMargins": True}),
        "timeRestore": False,
        "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps(
            {"query": {"query": "", "language": "kuery"}, "filter": []})}},
        "references": [{"name": "b1:panel_b1", "type": "lens", "id": LENS_BACKEND}]}


def create_saved_objects():
    objs = [
        _data_view(DV_FRONTEND, "frontend-*", "frontend logs"),
        _data_view(DV_BILLING, "billing-*", "billing logs"),
        _data_view(DV_BACKEND, "backend-*", "backend logs"),
        _lens_formbased(LENS_5XX, "5xx by service", DV_FRONTEND),
        _lens_formbased(LENS_BACKEND, "Backend errors over time", DV_BACKEND, field="host"),
        _legacy_viz(),
        _saved_search(),
        _dashboard_checkout(),
        _dashboard_backend(),
    ]
    res = req(KB, "POST", "/api/saved_objects/_bulk_create?overwrite=true", objs)
    errors = [o for o in (res.get("saved_objects") or []) if o.get("error")]
    if errors:
        raise RuntimeError(f"saved object create errors: {errors[:2]}")
    print(f"saved objects created: {len(objs)} "
          f"(2 dashboards, 1 saved search, 2 lens, 1 viz, 3 data views)")


def install_sample_flights():
    """Elastic-authored dashboard estate — a parser stress test."""
    try:
        req(KB, "POST", "/api/sample_data/flights")
        print("flights sample data installed (Elastic-authored dashboards)")
    except Exception as e:
        print(f"(flights sample data skipped: {e})")


if __name__ == "__main__":
    ensure_kibana_system_password()
    wait_kibana()
    seed_recent()
    create_saved_objects()
    install_sample_flights()
    print("DONE_KIBANA_SEED")

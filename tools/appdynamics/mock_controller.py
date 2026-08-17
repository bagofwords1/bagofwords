"""Mock AppDynamics Controller — request/response shapes per the 21.x REST API.

There is no lightweight real controller (licensed binaries, GBs of RAM), so —
like tools/splunk/wildcard_guard_proxy.py — this simulates the exact surface
`AppDynamicsClient` touches, with JSON shapes taken from the 21.x docs' sample
payloads:

  GET  /controller/rest/applications
  GET  /controller/rest/applications/{app}/tiers|nodes|backends|business-transactions
  GET  /controller/rest/applications/{app}/metrics?metric-path=...        (browse)
  GET  /controller/rest/applications/{app}/metric-data?...                (series)
  GET  /controller/rest/applications/{app}/events
  GET  /controller/rest/applications/{app}/problems/healthrule-violations
  GET  /controller/rest/applications/{app}/request-snapshots
  POST /controller/api/oauth/access_token                                  (OAuth)

Faithfulness rules enforced on purpose:
  - `output=JSON` is REQUIRED: without it the controller answers XML — so does
    this mock, which makes a client that forgets the param fail loudly.
  - Basic auth logins must be account-qualified: `user@customer1`. A bare
    `user` is a 401 (this pins the client's @-append rule).
  - OAuth tokens expire (default 300s, override APPD_MOCK_TOKEN_TTL to make
    refresh-on-401 observable in seconds).
  - The empty-grant account authenticates fine but sees zero applications.

Seeded estate: a small bank — `retail-banking` and `mobile-banking` — with
tiers, nodes, backends, BTs, External Calls metric branches (the service map),
deterministic metric series, events, violations and snapshots.

Run:  uv run --project backend uvicorn mock_controller:app --port 8090
      (from this directory; or via docker-compose.yaml)
Creds: admin@customer1 / Secret123!   (or bare username `admin` + the client's
       auto-append), OAuth client bow_api@customer1 / OauthSecret456!,
       empty-world account `emptyuser` / Empty123!
"""
import base64
import hashlib
import os
import secrets
import time
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

ACCOUNT = "customer1"
USERS = {
    f"admin@{ACCOUNT}": "Secret123!",
    f"svc_bow@{ACCOUNT}": "Secret123!",
    f"emptyuser@{ACCOUNT}": "Empty123!",   # authenticates, sees zero apps
}
OAUTH_CLIENTS = {f"bow_api@{ACCOUNT}": "OauthSecret456!"}
TOKEN_TTL = int(os.environ.get("APPD_MOCK_TOKEN_TTL", "300"))
_tokens: dict = {}  # token -> expiry epoch

app = FastAPI(title="Mock AppDynamics Controller (21.x shapes)")


# ── seeded estate ─────────────────────────────────────────────────────────────

APPLICATIONS = [
    {"id": 100, "name": "retail-banking", "description": "Customer-facing retail banking"},
    {"id": 200, "name": "mobile-banking", "description": "Mobile channel"},
]

TIERS = {
    100: [
        {"id": 10, "name": "web-portal", "type": "Application Server", "agentType": "APP_AGENT", "numberOfNodes": 4},
        {"id": 11, "name": "api-services", "type": "Application Server", "agentType": "APP_AGENT", "numberOfNodes": 6},
        {"id": 12, "name": "payments", "type": "Application Server", "agentType": "APP_AGENT", "numberOfNodes": 3},
    ],
    200: [
        {"id": 20, "name": "mobile-gateway", "type": "Application Server", "agentType": "APP_AGENT", "numberOfNodes": 4},
        {"id": 21, "name": "notifications", "type": "Application Server", "agentType": "APP_AGENT", "numberOfNodes": 2},
    ],
}

NODES = {
    100: [
        {"id": 1001, "name": "web-portal-node-1", "tierId": 10, "tierName": "web-portal", "machineName": "bnk-web-01", "agentType": "APP_AGENT", "appAgentVersion": "Server Agent v21.4.0"},
        {"id": 1002, "name": "web-portal-node-2", "tierId": 10, "tierName": "web-portal", "machineName": "bnk-web-02", "agentType": "APP_AGENT", "appAgentVersion": "Server Agent v21.4.0"},
        {"id": 1101, "name": "api-services-node-1", "tierId": 11, "tierName": "api-services", "machineName": "bnk-api-01", "agentType": "APP_AGENT", "appAgentVersion": "Server Agent v21.4.0"},
        {"id": 1102, "name": "api-services-node-2", "tierId": 11, "tierName": "api-services", "machineName": "bnk-api-02", "agentType": "APP_AGENT", "appAgentVersion": "Server Agent v21.4.0"},
        {"id": 1201, "name": "payments-node-1", "tierId": 12, "tierName": "payments", "machineName": "bnk-pay-01", "agentType": "APP_AGENT", "appAgentVersion": "Server Agent v21.4.0"},
    ],
    200: [
        {"id": 2001, "name": "mobile-gateway-node-1", "tierId": 20, "tierName": "mobile-gateway", "machineName": "bnk-mob-01", "agentType": "APP_AGENT", "appAgentVersion": "Server Agent v21.4.0"},
        {"id": 2101, "name": "notifications-node-1", "tierId": 21, "tierName": "notifications", "machineName": "bnk-not-01", "agentType": "APP_AGENT", "appAgentVersion": "Server Agent v21.4.0"},
    ],
}

BACKENDS = {
    100: [
        {"id": 501, "name": "retailbank-oracle-db", "exitPointType": "JDBC"},
        {"id": 502, "name": "redis-cache", "exitPointType": "CUSTOM"},
        {"id": 503, "name": "SWIFT-gateway", "exitPointType": "HTTP"},
        {"id": 504, "name": "fraud-scoring-service", "exitPointType": "HTTP"},
    ],
    200: [
        {"id": 601, "name": "mobilebank-postgres-db", "exitPointType": "JDBC"},
        {"id": 602, "name": "push-provider", "exitPointType": "HTTP"},
        {"id": 603, "name": "api-services-x-app", "exitPointType": "HTTP"},
    ],
}

BUSINESS_TRANSACTIONS = {
    100: [
        {"id": 9001, "name": "/login", "tierName": "web-portal", "entryPointType": "SERVLET", "internalName": "/login"},
        {"id": 9002, "name": "/accounts/balance", "tierName": "api-services", "entryPointType": "SERVLET", "internalName": "/accounts/balance"},
        {"id": 9003, "name": "/transfer", "tierName": "payments", "entryPointType": "SERVLET", "internalName": "/transfer"},
        {"id": 9004, "name": "/cards/statement", "tierName": "api-services", "entryPointType": "SERVLET", "internalName": "/cards/statement"},
    ],
    200: [
        {"id": 9101, "name": "/mobile/login", "tierName": "mobile-gateway", "entryPointType": "SERVLET", "internalName": "/mobile/login"},
        {"id": 9102, "name": "/mobile/push", "tierName": "notifications", "entryPointType": "SERVLET", "internalName": "/mobile/push"},
    ],
}

# The service map, expressed exactly the way the real controller does: as the
# children of `Overall Application Performance|<tier>|External Calls`.
EXTERNAL_CALLS = {
    (100, "web-portal"): ['Call-HTTP to api-services'],
    (100, "api-services"): [
        'Call-JDBC to Discovered backend call "retailbank-oracle-db"',
        'Call-CUSTOM to Discovered backend call "redis-cache"',
        'Call-HTTP to payments',
    ],
    (100, "payments"): [
        'Call-HTTP to Discovered backend call "SWIFT-gateway"',
        'Call-HTTP to Discovered backend call "fraud-scoring-service"',
        'Call-JDBC to Discovered backend call "retailbank-oracle-db"',
    ],
    (200, "mobile-gateway"): [
        'Call-JDBC to Discovered backend call "mobilebank-postgres-db"',
        'Call-HTTP to notifications',
        'Call-HTTP to Discovered backend call "api-services-x-app"',
    ],
    (200, "notifications"): ['Call-HTTP to Discovered backend call "push-provider"'],
}

# Baseline avg-response-time (ms) / calls-per-minute per BT — payments is slow.
BT_BASELINES = {
    "/login": (180, 420), "/accounts/balance": (95, 900), "/transfer": (850, 130),
    "/cards/statement": (240, 210), "/mobile/login": (150, 610), "/mobile/push": (60, 1500),
}

EVENTS = {
    100: [
        {"id": 70001, "type": "POLICY_OPEN_CRITICAL", "severity": "ERROR",
         "summary": "Health Rule 'Transfer response time' is critical on payments",
         "eventTime": None, "deepLinkUrl": "", "affectedEntities": [{"entityType": "APPLICATION_COMPONENT", "name": "payments"}]},
        {"id": 70002, "type": "APP_SERVER_RESTART", "severity": "WARN",
         "summary": "App server restart detected on bnk-pay-01",
         "eventTime": None, "deepLinkUrl": "", "affectedEntities": [{"entityType": "APPLICATION_COMPONENT_NODE", "name": "payments-node-1"}]},
        {"id": 70003, "type": "DEPLOYMENT", "severity": "INFO",
         "summary": "Deployment: api-services v2.14.1 rolled out",
         "eventTime": None, "deepLinkUrl": "", "affectedEntities": [{"entityType": "APPLICATION_COMPONENT", "name": "api-services"}]},
    ],
    200: [
        {"id": 71001, "type": "POLICY_OPEN_WARNING", "severity": "WARN",
         "summary": "Health Rule 'Push latency' warning on notifications",
         "eventTime": None, "deepLinkUrl": "", "affectedEntities": [{"entityType": "APPLICATION_COMPONENT", "name": "notifications"}]},
    ],
}

VIOLATIONS = {
    100: [
        {"id": 80001, "name": "Transfer response time", "severity": "CRITICAL",
         "incidentStatus": "OPEN", "startTimeInMillis": None, "endTimeInMillis": -1,
         "affectedEntityDefinition": {"entityType": "BUSINESS_TRANSACTION", "name": "/transfer", "entityId": 9003},
         "detectedTimeInMillis": None, "deepLinkUrl": ""},
        {"id": 80002, "name": "JVM heap on api-services", "severity": "WARNING",
         "incidentStatus": "RESOLVED", "startTimeInMillis": None, "endTimeInMillis": None,
         "affectedEntityDefinition": {"entityType": "APPLICATION_COMPONENT", "name": "api-services", "entityId": 11},
         "detectedTimeInMillis": None, "deepLinkUrl": ""},
    ],
    200: [
        {"id": 81001, "name": "Push latency", "severity": "WARNING",
         "incidentStatus": "OPEN", "startTimeInMillis": None, "endTimeInMillis": -1,
         "affectedEntityDefinition": {"entityType": "APPLICATION_COMPONENT", "name": "notifications", "entityId": 21},
         "detectedTimeInMillis": None, "deepLinkUrl": ""},
    ],
}

SNAPSHOTS = {
    100: [
        {"requestGUID": "guid-tx-0001", "businessTransaction": "/transfer", "applicationComponent": "payments",
         "applicationComponentNode": "payments-node-1", "userExperience": "VERY_SLOW",
         "timeTakenInMilliSecs": 4120, "errorOccured": False, "localStartTime": None,
         "summary": "Slow JDBC call to retailbank-oracle-db (3.1s)"},
        {"requestGUID": "guid-tx-0002", "businessTransaction": "/transfer", "applicationComponent": "payments",
         "applicationComponentNode": "payments-node-1", "userExperience": "ERROR",
         "timeTakenInMilliSecs": 950, "errorOccured": True, "localStartTime": None,
         "summary": "HTTP 502 from SWIFT-gateway"},
        {"requestGUID": "guid-tx-0003", "businessTransaction": "/accounts/balance", "applicationComponent": "api-services",
         "applicationComponentNode": "api-services-node-2", "userExperience": "SLOW",
         "timeTakenInMilliSecs": 780, "errorOccured": False, "localStartTime": None,
         "summary": "redis-cache miss storm"},
    ],
    200: [
        {"requestGUID": "guid-mb-0001", "businessTransaction": "/mobile/push", "applicationComponent": "notifications",
         "applicationComponentNode": "notifications-node-1", "userExperience": "SLOW",
         "timeTakenInMilliSecs": 640, "errorOccured": False, "localStartTime": None,
         "summary": "push-provider throttling"},
    ],
}


# ── auth ──────────────────────────────────────────────────────────────────────

def _check_auth(request: Request) -> Optional[Response]:
    """Return an error Response, or None when authenticated. Sets
    request.state.login to the authenticated principal."""
    auth = request.headers.get("authorization") or ""
    if auth.startswith("Basic "):
        try:
            user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
        except Exception:
            return JSONResponse({"error": "bad authorization header"}, status_code=401)
        # Account-qualified logins ONLY — a bare username is a 401, exactly
        # like the real controller. This pins the client's @-append rule.
        if USERS.get(user) != pw:
            return JSONResponse({"error": "invalid credentials"}, status_code=401)
        request.state.login = user
        return None
    if auth.startswith("Bearer "):
        token = auth[7:]
        exp = _tokens.get(token)
        if not exp or exp < time.time():
            _tokens.pop(token, None)
            return JSONResponse({"error": "token expired or unknown"}, status_code=401)
        request.state.login = f"bow_api@{ACCOUNT}"
        return None
    return JSONResponse({"error": "authentication required"}, status_code=401)


def _require_json(request: Request) -> Optional[Response]:
    """The real controller answers XML unless output=JSON is passed."""
    if request.query_params.get("output") != "JSON":
        return Response(
            content="<applications><!-- pass output=JSON for JSON --></applications>",
            media_type="text/xml",
        )
    return None


def _visible_apps(request: Request):
    if getattr(request.state, "login", "").startswith("emptyuser@"):
        return []
    return APPLICATIONS


def _resolve_app(request: Request, app_ref: str) -> Optional[dict]:
    for a in _visible_apps(request):
        if str(a["id"]) == app_ref or a["name"] == app_ref:
            return a
    return None


@app.post("/controller/api/oauth/access_token")
async def oauth_token(request: Request):
    form = await request.form()
    if form.get("grant_type") != "client_credentials":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    client_id = form.get("client_id") or ""
    if OAUTH_CLIENTS.get(client_id) != form.get("client_secret"):
        return JSONResponse({"error": "invalid_client"}, status_code=401)
    token = secrets.token_urlsafe(24)
    _tokens[token] = time.time() + TOKEN_TTL
    return {"access_token": token, "expires_in": TOKEN_TTL, "token_type": "Bearer"}


# ── REST resources ────────────────────────────────────────────────────────────

def _guard(request: Request) -> Optional[Response]:
    return _check_auth(request) or _require_json(request)


@app.get("/controller/rest/applications")
async def applications(request: Request):
    if (err := _guard(request)) is not None:
        return err
    return _visible_apps(request)


@app.get("/controller/rest/applications/{app_ref}/tiers")
async def tiers(request: Request, app_ref: str):
    if (err := _guard(request)) is not None:
        return err
    a = _resolve_app(request, app_ref)
    return TIERS[a["id"]] if a else JSONResponse({"error": "application not found"}, status_code=404)


@app.get("/controller/rest/applications/{app_ref}/nodes")
async def nodes(request: Request, app_ref: str):
    if (err := _guard(request)) is not None:
        return err
    a = _resolve_app(request, app_ref)
    return NODES[a["id"]] if a else JSONResponse({"error": "application not found"}, status_code=404)


@app.get("/controller/rest/applications/{app_ref}/backends")
async def backends(request: Request, app_ref: str):
    if (err := _guard(request)) is not None:
        return err
    a = _resolve_app(request, app_ref)
    return BACKENDS[a["id"]] if a else JSONResponse({"error": "application not found"}, status_code=404)


@app.get("/controller/rest/applications/{app_ref}/business-transactions")
async def business_transactions(request: Request, app_ref: str):
    if (err := _guard(request)) is not None:
        return err
    a = _resolve_app(request, app_ref)
    return BUSINESS_TRANSACTIONS[a["id"]] if a else JSONResponse({"error": "application not found"}, status_code=404)


@app.get("/controller/rest/applications/{app_ref}/metrics")
async def metric_browse(request: Request, app_ref: str):
    """Metric-tree browse: folders + leaf metrics, 21.x shape
    ({"name": ..., "type": "folder"|"leaf"})."""
    if (err := _guard(request)) is not None:
        return err
    a = _resolve_app(request, app_ref)
    if not a:
        return JSONResponse({"error": "application not found"}, status_code=404)
    path = request.query_params.get("metric-path") or ""
    segs = [s for s in path.split("|") if s] if path else []
    tier_names = [t["name"] for t in TIERS[a["id"]]]

    if not segs:
        return [{"name": "Overall Application Performance", "type": "folder"},
                {"name": "Business Transaction Performance", "type": "folder"},
                {"name": "Application Infrastructure Performance", "type": "folder"}]
    if segs[0] == "Overall Application Performance":
        if len(segs) == 1:
            return [{"name": t, "type": "folder"} for t in tier_names]
        if len(segs) == 2 and segs[1] in tier_names:
            return [{"name": "External Calls", "type": "folder"},
                    {"name": "Average Response Time (ms)", "type": "leaf"},
                    {"name": "Calls per Minute", "type": "leaf"},
                    {"name": "Errors per Minute", "type": "leaf"}]
        if len(segs) == 3 and segs[1] in tier_names and segs[2] == "External Calls":
            calls = EXTERNAL_CALLS.get((a["id"], segs[1]), [])
            return [{"name": c, "type": "folder"} for c in calls]
        if len(segs) == 4 and segs[2] == "External Calls":
            return [{"name": "Calls per Minute", "type": "leaf"},
                    {"name": "Average Response Time (ms)", "type": "leaf"},
                    {"name": "Errors per Minute", "type": "leaf"}]
    if segs[0] == "Business Transaction Performance":
        if len(segs) == 1:
            return [{"name": "Business Transactions", "type": "folder"}]
        if len(segs) == 2:
            return [{"name": t, "type": "folder"} for t in tier_names]
        if len(segs) == 3:
            bts = [b for b in BUSINESS_TRANSACTIONS[a["id"]] if segs[2] in ("*", b["tierName"])]
            return [{"name": b["name"], "type": "folder"} for b in bts]
        if len(segs) == 4:
            return [{"name": "Average Response Time (ms)", "type": "leaf"},
                    {"name": "Calls per Minute", "type": "leaf"},
                    {"name": "Errors per Minute", "type": "leaf"}]
    return []


def _series_points(seed: str, base: float, minutes: int, freq_mins: int = 1):
    """Deterministic pseudo-series: value wobbles ±25% around base."""
    now_ms = int(time.time() * 1000)
    points = []
    n = max(1, min(minutes // freq_mins, 360))
    for i in range(n):
        h = int(hashlib.sha256(f"{seed}:{i}".encode()).hexdigest()[:8], 16)
        wobble = (h % 1000) / 1000.0 - 0.5          # -0.5..0.5
        value = max(1, base * (1 + 0.5 * wobble))
        start = now_ms - (n - i) * freq_mins * 60_000
        points.append({
            "startTimeInMillis": start,
            "value": round(value, 1),
            "min": round(value * 0.6, 1),
            "max": round(value * 1.7, 1),
            "sum": round(value * freq_mins, 1),
            "count": freq_mins,
            "current": round(value, 1),
            "occurrences": 1,
            "standardDeviation": 0.0,
            "useRange": True,
        })
    return points


def _metric_instances(a: dict, path: str):
    """Expand a (possibly wildcarded) metric path into concrete series specs."""
    segs = path.split("|")
    out = []
    tier_names = [t["name"] for t in TIERS[a["id"]]]

    def bt_metric(bt, metric):
        base_rt, base_cpm = BT_BASELINES.get(bt["name"], (200, 300))
        base = {"Average Response Time (ms)": base_rt, "Calls per Minute": base_cpm,
                "Errors per Minute": max(1, base_cpm // 50)}.get(metric)
        if base is None:
            return None
        full = f"Business Transaction Performance|Business Transactions|{bt['tierName']}|{bt['name']}|{metric}"
        return (full, metric, base)

    if segs[0] == "Business Transaction Performance" and len(segs) == 5:
        _, _, tier_seg, bt_seg, metric = segs
        for bt in BUSINESS_TRANSACTIONS[a["id"]]:
            if tier_seg not in ("*", bt["tierName"]):
                continue
            if bt_seg not in ("*", bt["name"]):
                continue
            spec = bt_metric(bt, metric)
            if spec:
                out.append(spec)
    elif segs[0] == "Overall Application Performance":
        if len(segs) == 3:  # ...|<tier>|<metric>
            _, tier_seg, metric = segs
            for t in tier_names:
                if tier_seg in ("*", t):
                    base = {"Average Response Time (ms)": 200, "Calls per Minute": 800,
                            "Errors per Minute": 12}.get(metric)
                    if base:
                        out.append((f"Overall Application Performance|{t}|{metric}", metric, base))
        if len(segs) == 5 and segs[2] == "External Calls":  # ...|<tier>|External Calls|<edge>|<metric>
            _, tier_seg, _, edge_seg, metric = segs
            for (app_id, tname), calls in EXTERNAL_CALLS.items():
                if app_id != a["id"] or tier_seg not in ("*", tname):
                    continue
                for c in calls:
                    if edge_seg in ("*", c):
                        h = int(hashlib.sha256(c.encode()).hexdigest()[:4], 16)
                        base = {"Calls per Minute": 100 + h % 900,
                                "Average Response Time (ms)": 20 + h % 300,
                                "Errors per Minute": 1 + h % 10}.get(metric)
                        if base:
                            out.append((f"Overall Application Performance|{tname}|External Calls|{c}|{metric}", metric, base))
    elif segs[0] == "Application Infrastructure Performance" and len(segs) >= 4:
        tier_seg = segs[1]
        metric = segs[-1]
        for t in tier_names:
            if tier_seg in ("*", t) and metric == "%Busy":
                out.append((f"Application Infrastructure Performance|{t}|Hardware Resources|CPU|%Busy", "%Busy", 35))
    return out


@app.get("/controller/rest/applications/{app_ref}/metric-data")
async def metric_data(request: Request, app_ref: str):
    if (err := _guard(request)) is not None:
        return err
    a = _resolve_app(request, app_ref)
    if not a:
        return JSONResponse({"error": "application not found"}, status_code=404)
    path = request.query_params.get("metric-path") or ""
    rollup = (request.query_params.get("rollup") or "false").lower() == "true"
    minutes = int(request.query_params.get("duration-in-mins") or 60)
    series = []
    for i, (full_path, metric, base) in enumerate(_metric_instances(a, path)):
        points = _series_points(full_path, base, minutes)
        if rollup and points:
            vals = [p["value"] for p in points]
            points = [{**points[-1], "value": round(sum(vals) / len(vals), 1),
                       "min": round(min(vals), 1), "max": round(max(vals), 1),
                       "sum": round(sum(vals), 1), "count": len(vals)}]
        series.append({
            "metricId": 30000 + i,
            "metricName": metric,
            "metricPath": full_path,
            "frequency": "ONE_MIN",
            "metricValues": points,
        })
    return series


def _stamp(rows, key, offset_mins=30):
    now_ms = int(time.time() * 1000)
    out = []
    for i, r in enumerate(rows):
        r = dict(r)
        if r.get(key) is None:
            r[key] = now_ms - (i + 1) * offset_mins * 60_000
        if "detectedTimeInMillis" in r and r["detectedTimeInMillis"] is None:
            r["detectedTimeInMillis"] = r[key]
        if "endTimeInMillis" in r and r["endTimeInMillis"] is None:
            r["endTimeInMillis"] = r[key] + 20 * 60_000
        out.append(r)
    return out


@app.get("/controller/rest/applications/{app_ref}/events")
async def events(request: Request, app_ref: str):
    if (err := _guard(request)) is not None:
        return err
    a = _resolve_app(request, app_ref)
    if not a:
        return JSONResponse({"error": "application not found"}, status_code=404)
    # The real API rejects unfiltered calls — enforce the same contract.
    if not request.query_params.get("event-types") or not request.query_params.get("severities"):
        return JSONResponse({"error": "event-types and severities are required"}, status_code=400)
    sevs = set((request.query_params.get("severities") or "").split(","))
    rows = [e for e in _stamp(EVENTS[a["id"]], "eventTime") if e["severity"] in sevs]
    return rows


@app.get("/controller/rest/applications/{app_ref}/problems/healthrule-violations")
async def violations(request: Request, app_ref: str):
    if (err := _guard(request)) is not None:
        return err
    a = _resolve_app(request, app_ref)
    if not a:
        return JSONResponse({"error": "application not found"}, status_code=404)
    return _stamp(VIOLATIONS[a["id"]], "startTimeInMillis")


@app.get("/controller/rest/applications/{app_ref}/request-snapshots")
async def snapshots(request: Request, app_ref: str):
    if (err := _guard(request)) is not None:
        return err
    a = _resolve_app(request, app_ref)
    if not a:
        return JSONResponse({"error": "application not found"}, status_code=404)
    return _stamp(SNAPSHOTS[a["id"]], "localStartTime")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("APPD_MOCK_PORT", "8090")))

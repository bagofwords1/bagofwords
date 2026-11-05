"""After-fix checks that have no 'before' script: heartbeat at the collector, platform stream delivery of an
unknown-account failure over HTTP, request id echo, envelope contract fields, health endpoint."""
import sys, os, json, time; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
H = hdr(ADMIN["token"], ua="audit-loop/after")
out = {}
r = api.post("/api/api_keys", headers={**H, "X-Request-ID": "loop-after-0001-abcdef"}, json={"name": "after-rid"})
out["request_id_echo"] = r.headers.get("X-Request-ID")
ghost = "ghost-after@example.com"
out["ghost_login"] = api.post("/api/auth/jwt/login", data={"username": ghost, "password": "nope-1"}, headers={"User-Agent": "audit-loop/after"}).status_code
time.sleep(40)  # heartbeat 30 s + export tick
ev = [e for e in events(since=0) if not e["dup"]]
by = {}
for e in ev: by.setdefault(e["event"]["action"], []).append(e)
rid = [e["event"] for e in by.get("api_key.created", []) if e["event"]["metadata"].get("name") == "after-rid"]
out["api_key_event"] = {k: rid[0].get(k) for k in ("outcome", "schema_version", "legacy", "recorded_at", "occurred_at", "correlation", "context", "actor", "category")} if rid else None
hb = [e for e in by.get("audit.heartbeat", [])]
out["heartbeats_received"] = len(hb); out["heartbeat_sample"] = hb[0]["event"]["metadata"] if hb else None
plat = [e for e in by.get("auth.login_failed", []) if e["event"]["metadata"].get("email") == ghost]
out["platform_delivery"] = [{"intake": e["intake"], "hec_index": None, "organization": e["event"]["organization"], "actor": e["event"]["actor"], "outcome": e["event"]["outcome"], "failure_reason": e["event"]["failure_reason"], "ip": e["event"]["context"]["ip_address"]} for e in plat]
out["ghost_in_org_intakes"] = [e["intake"] for e in plat if e["intake"] == "webhook"]
h = api.get("/api/enterprise/audit/health", headers=H); out["health_status"] = h.status_code
hj = h.json(); out["health"] = {"recording": hj["recording"], "contract": {k: hj["contract"][k] for k in ("records_missing_outcome", "legacy_records")}, "heartbeat": hj["heartbeat"], "streams": [{k: s[k] for k in ("name", "state", "pending", "lag_seconds", "consecutive_failures")} for s in hj["streams"]]}
st = stats(); out["collector_invalid_events"] = st["invalid_events"]; out["collector_invalid_samples"] = st["invalid_samples"][:3]
pp(out); json.dump(out, open(os.path.join(S, "11_after_result.json"), "w"), indent=1, default=str)

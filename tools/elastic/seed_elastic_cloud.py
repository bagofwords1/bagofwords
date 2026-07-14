#!/usr/bin/env python3
"""Seed an Elastic Cloud *serverless* project with a log-investigation dataset.

Adapted from tools/elastic/seed_elastic.py for the cloud onboarding API key,
which only allows `create_doc` into logs-*-* data streams:
  - no index templates (serverless logs-*-* template applies automatically)
  - no custom index names -> data streams logs-<service>-default
  - ApiKey auth, HTTPS via the sandbox proxy + CA bundle
  - dates end today so "last 24h" style questions work
  - a deliberate incident is injected for root-cause analysis:
      2026-07-14 09:12-10:45 UTC: backend db pool exhausted on host-03
      -> backend error spike, frontend 502s from upstream timeouts

Reads ES_URL and ES_API_KEY_ENCODED from env (ES_CA_BUNDLE optionally points
at a custom CA, e.g. a sandbox egress proxy's). Never prints the key.
"""
import json
import os
import random
import ssl
import urllib.request

ES = os.environ["ES_URL"].rstrip("/")
KEY = os.environ["ES_API_KEY_ENCODED"]
CTX = ssl.create_default_context(cafile=os.environ.get("ES_CA_BUNDLE") or None)
random.seed(1337)

SERVICES = ["frontend", "backend", "payments", "checkout", "auth", "search"]
DAYS = ["2026-07-10", "2026-07-11", "2026-07-12", "2026-07-13", "2026-07-14"]
HOSTS = [f"host-{i:02d}" for i in range(1, 9)]
LEVELS = ["info"] * 12 + ["warn"] * 3 + ["error"]
MESSAGES = {
    "info": ["request handled", "cache hit", "user login ok", "healthcheck ok",
             "session refreshed", "order created"],
    "warn": ["slow response", "retry scheduled", "cache miss", "high latency"],
    "error": ["failed to look up user", "connection refused", "timeout talking to upstream",
              "internal error while processing request"],
}
PER_SERVICE_PER_DAY = 260   # ~7.8k baseline docs


def bulk(lines):
    body = ("\n".join(lines) + "\n").encode()
    req = urllib.request.Request(
        f"{ES}/_bulk", data=body, method="POST",
        headers={"Content-Type": "application/x-ndjson",
                 "Authorization": f"ApiKey {KEY}"})
    with urllib.request.urlopen(req, timeout=120, context=CTX) as resp:
        out = json.loads(resp.read())
    if out.get("errors"):
        errs = [i for i in out["items"] if i["create"].get("error")][:3]
        raise RuntimeError(f"bulk errors: {json.dumps(errs)[:600]}")
    return len(out["items"])


def doc(service, day, hour=None, level=None, message=None, host=None,
        status=None, latency=None, extra=None):
    level = level or random.choice(LEVELS)
    message = message or random.choice(MESSAGES[level])
    hour = hour if hour is not None else random.randint(0, 23)
    if day == "2026-07-14":  # today: keep within the past (<= 17:00 UTC)
        hour = min(hour, 16)
    ts = f"{day}T{hour:02d}:{random.randint(0,59):02d}:{random.randint(0,59):02d}.{random.randint(0,999):03d}Z"
    if status is None:
        status = random.choice([200, 200, 200, 200, 201, 301, 404]) if level != "error" \
            else random.choice([500, 502, 503])
    d = {
        "@timestamp": ts,
        "message": message,
        "log.level": level,
        "service.name": service,
        "host.name": host or random.choice(HOSTS),
        "http.response.status_code": status,
        "event.duration_ms": latency if latency is not None else round(random.uniform(2, 900), 1),
        "user.id": f"u{random.randint(1, 400)}",
    }
    if level == "error":
        d["error.message"] = message
    if extra:
        d.update(extra)
    return d


def emit(buf, service, d, counters):
    buf.append(json.dumps({"create": {"_index": f"logs-{service}-default"}}))
    buf.append(json.dumps(d))
    counters[service] = counters.get(service, 0) + 1


def main():
    buf, counters, sent = [], {}, 0

    # --- baseline traffic ---
    for service in SERVICES:
        for day in DAYS:
            for _ in range(PER_SERVICE_PER_DAY):
                emit(buf, service, doc(service, day), counters)
                if len(buf) >= 4000:
                    sent += bulk(buf); buf = []; print(f"  ...{sent} docs")

    # --- the incident: 2026-07-14 09:12-10:45 UTC ---
    # Root cause: backend on host-03 exhausts its db connection pool.
    for _ in range(420):
        minute = random.randint(12, 105)  # offset from 09:00
        h, m = 9 + minute // 60, minute % 60
        ts = f"2026-07-14T{h:02d}:{m:02d}:{random.randint(0,59):02d}.{random.randint(0,999):03d}Z"
        d = doc("backend", "2026-07-14", hour=h, level="error", host="host-03",
                message="db connection pool exhausted: timed out waiting for connection (pool size 20)",
                status=500, latency=round(random.uniform(5000, 30000), 1),
                extra={"error.type": "PoolTimeoutError", "db.name": "orders_db"})
        d["@timestamp"] = ts
        emit(buf, "backend", d, counters)
    # Cascade: frontend 502s about backend upstream.
    for _ in range(300):
        minute = random.randint(14, 108)
        h, m = 9 + minute // 60, minute % 60
        ts = f"2026-07-14T{h:02d}:{m:02d}:{random.randint(0,59):02d}.{random.randint(0,999):03d}Z"
        d = doc("frontend", "2026-07-14", hour=h, level="error",
                message="upstream timeout: backend did not respond within 10s",
                status=502, latency=10000.0,
                extra={"error.type": "UpstreamTimeout", "upstream.service": "backend"})
        d["@timestamp"] = ts
        emit(buf, "frontend", d, counters)
    # A deploy marker just before the incident (the smoking gun).
    d = doc("backend", "2026-07-14", hour=9, level="info", host="host-03",
            message="deployment finished: backend v2.14.0 (config change: db pool size 50 -> 20)",
            status=200, latency=1.0, extra={"event.action": "deploy", "service.version": "2.14.0"})
    d["@timestamp"] = "2026-07-14T09:08:41.000Z"
    emit(buf, "backend", d, counters)

    if buf:
        sent += bulk(buf)
    print(f"indexed {sent} docs total")
    for s in sorted(counters):
        print(f"  logs-{s}-default: {counters[s]}")
    print("DONE_ELASTIC_SEED")


if __name__ == "__main__":
    main()

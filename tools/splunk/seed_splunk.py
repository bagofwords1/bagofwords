#!/usr/bin/env python3
"""Seed the local Splunk with a realistic, log-investigation dataset.

Creates a few indexes, opens the HEC token to them, and bulk-loads ~13k events
across several sourcetypes whose field shapes deliberately DIFFER (schema-on-
read). `web:access_combined` is the highest-volume sourcetype, so with a small
`max_sampled_sourcetypes` cap it is the one that gets its fields sampled, while
the lower-volume sourcetypes stay "thin" — exercising the unknown-schema path.

Usage:  python seed_splunk.py   (Splunk must be up on localhost:8089/8088)
Prints an auth token on the last line as: SPLUNK_TOKEN=<token>
"""
import json
import random
import ssl
import time
import urllib.request
import urllib.error

MGMT = "https://127.0.0.1:8089"
HEC = "https://127.0.0.1:8088"
HEC_TOKEN = "11111111-1111-1111-1111-111111111111"
USER, PWD = "admin", "Chang3d_pwd1"
random.seed(1337)

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=_CTX),
)

NOW = int(time.time())
DAYS = 7

# (index, sourcetype, volume_weight, field_generator)
def web_event():
    status = random.choice([200, 200, 200, 201, 301, 404, 500, 502, 503])
    return {
        "clientip": f"10.0.{random.randint(0,255)}.{random.randint(1,254)}",
        "method": random.choice(["GET", "GET", "POST", "PUT"]),
        "uri_path": random.choice(["/", "/login", "/api/orders", "/checkout", "/health"]),
        "status": status,
        "bytes": random.randint(80, 90000),
        "response_time": round(random.uniform(2, 3000), 1),
        "useragent": random.choice(["curl/8", "Mozilla/5.0", "okhttp/4"]),
    }

def log4j_event():
    lvl = random.choice(["INFO", "INFO", "INFO", "WARN", "ERROR", "ERROR", "FATAL"])
    return {
        "level": lvl,
        "logger": random.choice(["com.acme.OrderSvc", "com.acme.AuthSvc", "com.acme.Db"]),
        "thread": f"http-nio-{random.randint(1,50)}",
        "error_code": random.choice(["E1001", "E2002", "E5003"]) if lvl in ("ERROR", "FATAL") else "",
        "message": random.choice(["order processed", "cache miss", "failed to look up user",
                                  "connection refused", "internal error while processing"]),
    }

def json_app_event():
    lvl = random.choice(["info", "info", "warn", "error", "fatal"])
    return {
        "service": random.choice(["orders", "billing", "cart"]),
        "level": lvl,
        "latency_ms": round(random.uniform(1, 2500), 1),
        "user_id": f"u{random.randint(1,400)}",
        "trace_id": f"{random.randint(10**15, 10**16):x}",
        "internal_error": lvl in ("error", "fatal"),
    }

def auth_audit_event():
    return {
        "action": random.choice(["login", "logout", "password_reset", "mfa_challenge"]),
        "user": f"user{random.randint(1,80)}",
        "src_ip": f"192.168.{random.randint(0,255)}.{random.randint(1,254)}",
        "outcome": random.choice(["success", "success", "failure"]),
        "mfa": random.choice([True, False]),
    }

def metrics_event():
    return {
        "metric_name": random.choice(["cpu.pct", "mem.pct", "disk.io"]),
        "value": round(random.uniform(0, 100), 2),
        "collector": random.choice(["node-01", "node-02", "node-03"]),
    }

SOURCETYPES = [
    ("web", "access_combined", 6000, web_event),
    ("app", "log4j", 3000, log4j_event),
    ("app", "json_app", 2500, json_app_event),
    ("security", "auth_audit", 1000, auth_audit_event),
    ("metrics", "collectd", 500, metrics_event),
]
INDEXES = sorted({s[0] for s in SOURCETYPES})


def mgmt_req(method, path, data=None):
    body = urllib.parse.urlencode(data).encode() if data else None
    import base64
    tok = base64.b64encode(f"{USER}:{PWD}".encode()).decode()
    req = urllib.request.Request(f"{MGMT}{path}?output_mode=json", data=body, method=method,
                                 headers={"Authorization": f"Basic {tok}"})
    try:
        return json.loads(_OPENER.open(req, timeout=60).read() or "{}")
    except urllib.error.HTTPError as e:
        # 409 = already exists — fine.
        if e.code == 409:
            return {}
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read()[:300]}")


import urllib.parse


def wait_ready():
    for _ in range(60):
        try:
            mgmt_req("GET", "/services/server/info")
            print("splunk mgmt ready")
            return
        except Exception:
            time.sleep(3)
    raise RuntimeError("Splunk did not become ready")


def create_indexes():
    for idx in INDEXES:
        mgmt_req("POST", "/services/data/indexes", {"name": idx})
    print(f"indexes ensured: {', '.join(INDEXES)}")


def open_hec():
    # Allow the HEC token to write to our indexes (and enable HEC globally).
    mgmt_req("POST", "/servicesNS/nobody/splunk_httpinput/data/inputs/http/http",
             {"disabled": "0"})
    allow = ",".join(INDEXES + ["main"])
    try:
        mgmt_req("POST",
                 "/servicesNS/nobody/splunk_httpinput/data/inputs/http/splunk_hec",
                 {"indexes": allow, "index": "main"})
    except Exception as e:
        print(f"(hec token update note: {e})")
    print(f"HEC opened to indexes: {allow}")


def hec_send(batch):
    payload = "\n".join(json.dumps(e) for e in batch).encode()
    req = urllib.request.Request(f"{HEC}/services/collector/event", data=payload,
                                 headers={"Authorization": f"Splunk {HEC_TOKEN}"})
    for attempt in range(5):
        try:
            _OPENER.open(req, timeout=60).read()
            return
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HEC -> {e.code}: {e.read()[:300]}")
        except Exception:
            time.sleep(2)
    raise RuntimeError("HEC send failed after retries")


def seed_events():
    total = 0
    for index, sourcetype, weight, gen in SOURCETYPES:
        batch = []
        for _ in range(weight):
            ts = NOW - random.randint(0, DAYS * 86400)
            batch.append({
                "time": ts, "host": f"host-{random.randint(1,12):02d}",
                "source": f"/var/log/{sourcetype}.log",
                "sourcetype": sourcetype, "index": index,
                "event": gen(),
            })
            if len(batch) >= 1000:
                hec_send(batch); total += len(batch); batch = []
        if batch:
            hec_send(batch); total += len(batch)
        print(f"  sent {weight} {index}::{sourcetype}")
    print(f"seeded {total} events across {len(SOURCETYPES)} sourcetypes")


def make_token():
    # Mint an auth token (JWT) for the connector. Enable token auth first.
    try:
        mgmt_req("POST", "/services/admin/token-auth/tokens_auth", {"disabled": "0"})
    except Exception:
        pass
    try:
        res = mgmt_req("POST", "/services/authorization/tokens",
                       {"name": USER, "audience": "bow", "expires_on": "+30d"})
        entries = res.get("entry") or []
        tok = None
        if entries:
            tok = (entries[0].get("content") or {}).get("token")
        if tok:
            print(f"SPLUNK_TOKEN={tok}")
            return
    except Exception as e:
        print(f"(token mint note: {e})")
    print("SPLUNK_TOKEN=  (token auth unavailable — use admin/Chang3d_pwd1)")


if __name__ == "__main__":
    wait_ready()
    create_indexes()
    open_hec()
    seed_events()
    print("waiting for indexers to flush...")
    time.sleep(20)
    make_token()
    print("DONE_SPLUNK_SEED")

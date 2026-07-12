#!/usr/bin/env python3
"""Seed the local Elasticsearch with a realistic, log-investigation dataset.

Creates an index template (so keyword/date fields are typed, not auto-guessed)
and bulk-indexes ~13k log events across several service "patterns", each split
into per-day indices (`<service>-2026.07.10`, `…07.09`, …). The connector
collapses those daily indices into one `<service>-*` table — matching how log
investigation actually works.

Prints an API key on the last line as: ES_API_KEY=<id:key>

Usage:  python seed_elastic.py   (Elasticsearch must be up on localhost:9200)
"""
import base64
import json
import random
import time
import urllib.request
import urllib.error

ES = "http://127.0.0.1:9200"
AUTH = "elastic:elastic_pwd"
random.seed(1337)

# Force a direct (no-proxy) opener — the sandbox sets HTTPS_PROXY, which would
# otherwise hijack localhost calls and time out.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# Service prefixes mirror a typical log estate (and the product screenshot):
SERVICES = ["default", "storage", "backend", "frontend", "system",
            "edge", "network", "delivery", "security", "billing"]
DAYS = ["2026.07.06", "2026.07.07", "2026.07.08", "2026.07.09", "2026.07.10"]
HOSTS = [f"host-{i:02d}" for i in range(1, 13)]
LEVELS = ["info", "info", "info", "info", "warn", "warn", "error", "error", "fatal"]
SEV = {"info": "info", "warn": "notice", "error": "err", "fatal": "crit"}
MESSAGES = {
    "info": ["request handled", "cache hit", "user login ok", "healthcheck ok"],
    "warn": ["slow response", "retry scheduled", "cache miss", "high latency"],
    "error": ["failed to look up user", "connection refused", "timeout talking to upstream",
              "internal error while processing", "null pointer in handler"],
    "fatal": ["out of memory", "panic: unrecoverable state", "database unreachable"],
}
TOTAL = 13000


def req(method, path, body=None, headers=None, raw=False):
    url = f"{ES}{path}"
    data = None
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    if body is not None:
        data = body if raw else json.dumps(body).encode()
        if raw:
            data = body.encode() if isinstance(body, str) else body
    tok = base64.b64encode(AUTH.encode()).decode()
    h["Authorization"] = f"Basic {tok}"
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with _OPENER.open(r, timeout=60) as resp:
            return json.loads(resp.read() or "{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read()[:400]}")


def wait_green():
    for _ in range(60):
        try:
            h = req("GET", "/_cluster/health")
            if h.get("status") in ("yellow", "green"):
                print(f"cluster {h.get('status')}")
                return
        except Exception as e:
            pass
        time.sleep(2)
    raise RuntimeError("Elasticsearch did not become ready")


def create_template():
    req("PUT", "/_index_template/bow-logs", {
        "index_patterns": [f"{s}-*" for s in SERVICES],
        "template": {
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            "mappings": {
                "properties": {
                    "@timestamp": {"type": "date"},
                    "service": {"type": "keyword"},
                    "host": {"type": "keyword"},
                    "level": {"type": "keyword"},
                    "syslog_severity": {"type": "keyword"},
                    "status": {"type": "integer"},
                    "code_string": {"type": "keyword"},
                    "internal_error": {"type": "boolean"},
                    "err": {"type": "keyword"},
                    "latency_ms": {"type": "float"},
                    "user_id": {"type": "keyword"},
                    "message": {"type": "text",
                                "fields": {"keyword": {"type": "keyword", "ignore_above": 512}}},
                }
            },
        },
    })
    print("index template created")


def gen_doc(service, day):
    level = random.choice(LEVELS)
    msg = random.choice(MESSAGES[level])
    hour = random.randint(0, 23)
    minute = random.randint(0, 59)
    ts = f"{day.replace('.', '-')}T{hour:02d}:{minute:02d}:{random.randint(0,59):02d}Z"
    status = random.choice([200, 200, 200, 201, 301, 404, 500, 502, 503]) if level in ("info", "warn") \
        else random.choice([500, 502, 503, 500, 500])
    return {
        "@timestamp": ts,
        "service": service,
        "host": random.choice(HOSTS),
        "level": level,
        "syslog_severity": SEV[level],
        "status": status,
        "code_string": "Internal" if level in ("error", "fatal") else "OK",
        "internal_error": level in ("error", "fatal"),
        "err": msg if level in ("error", "fatal") else "",
        "latency_ms": round(random.uniform(2, 3000), 1),
        "user_id": f"u{random.randint(1, 400)}",
        "message": msg,
    }


def bulk_index():
    per = TOTAL // (len(SERVICES) * len(DAYS))
    buf = []
    n = 0
    for service in SERVICES:
        for day in DAYS:
            index = f"{service}-{day}"
            for _ in range(per):
                buf.append(json.dumps({"index": {"_index": index}}))
                buf.append(json.dumps(gen_doc(service, day)))
                n += 1
            if len(buf) >= 8000:
                req("POST", "/_bulk", "\n".join(buf) + "\n", raw=True)
                buf = []
    if buf:
        req("POST", "/_bulk", "\n".join(buf) + "\n", raw=True)
    req("POST", "/_refresh")
    print(f"indexed {n} log events across {len(SERVICES)} services x {len(DAYS)} days")


def make_api_key():
    res = req("POST", "/_security/api_key", {"name": "bow-demo", "role_descriptors": {}})
    # Encoded form the client accepts directly:
    encoded = res.get("encoded")
    pair = f"{res['id']}:{res['api_key']}"
    print(f"ES_API_KEY={pair}")
    print(f"ES_API_KEY_ENCODED={encoded}")


if __name__ == "__main__":
    wait_green()
    create_template()
    bulk_index()
    # quick sanity
    cnt = req("GET", "/security-*/_count")
    print(f"security-* count: {cnt.get('count')}")
    make_api_key()
    print("DONE_ELASTIC_SEED")

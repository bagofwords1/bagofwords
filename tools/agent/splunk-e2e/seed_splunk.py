#!/usr/bin/env python3
"""Seed a local Splunk container with LOTS of realistic machine data via HEC.

Creates indexes (web, app, security) through the management REST API, widens
the HEC token to those indexes, then pushes ~120k events spread over the last
72 hours:

  - web/access_combined   : nginx-style combined access logs (~60k)
  - app/app_logs          : JSON service logs with level/latency (~40k)
  - security/auth         : ssh/webauth login events (~20k)

A deliberate "incident" is baked in: 2 days ago 14:00-16:00 UTC the
payments service throws errors and the web tier returns 5xx, so analysis
prompts have a real signal to find.
"""
import gzip
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone

import requests
import urllib3

urllib3.disable_warnings()

MGMT = "https://localhost:8089"
HEC = "https://localhost:8088/services/collector/event"
ADMIN = ("admin", os.environ.get("SPLUNK_PASSWORD", "BowSplunk123!"))
HEC_TOKEN = os.environ.get("SPLUNK_HEC_TOKEN", "bow-hec-token-1234")

random.seed(1337)
now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
START = now - timedelta(hours=72)
INCIDENT_START = now - timedelta(hours=46)   # ~2 days ago
INCIDENT_END = INCIDENT_START + timedelta(hours=2)

# ── management setup ─────────────────────────────────────────────────────────
def mgmt_post(path, data):
    r = requests.post(f"{MGMT}{path}", data=data, auth=ADMIN, verify=False, timeout=60)
    return r

for idx in ("web", "app", "security"):
    r = mgmt_post("/services/data/indexes", {"name": idx, "output_mode": "json"})
    print("create index", idx, r.status_code)  # 409 = already exists, fine

# Widen the HEC token to the new indexes and disable its SSL-only quirks.
r = mgmt_post(
    "/servicesNS/nobody/splunk_httpinput/data/inputs/http/splunk_hec_token",
    {"indexes": "main,web,app,security", "index": "main", "output_mode": "json"},
)
print("update hec token", r.status_code, r.text[:200] if r.status_code >= 400 else "")

# ── event generators ─────────────────────────────────────────────────────────
HOSTS_WEB = [f"web-{i:02d}" for i in range(1, 7)]
SERVICES = ["payments", "checkout", "catalog", "auth-svc", "search", "cart"]
HOSTS_APP = {s: [f"{s}-{i}" for i in range(1, 4)] for s in SERVICES}
URIS = [
    ("/api/checkout", 0.10), ("/api/payment", 0.10), ("/api/products", 0.25),
    ("/api/search", 0.15), ("/api/cart", 0.12), ("/api/login", 0.08),
    ("/", 0.10), ("/static/app.js", 0.06), ("/health", 0.04),
]
URI_NAMES = [u for u, _ in URIS]
URI_W = [w for _, w in URIS]
AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) Firefox/126.0",
    "curl/8.5.0", "python-requests/2.32.0",
]
USERS = [f"user{i:03d}" for i in range(1, 201)]
ATTACK_IPS = ["45.155.205.99", "185.220.101.34", "91.240.118.172"]

def in_incident(ts):
    return INCIDENT_START <= ts <= INCIDENT_END

def rand_ip():
    return f"{random.randint(1,223)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}"

def diurnal(ts):
    """Traffic multiplier by hour of day (peak at 15:00 UTC)."""
    h = ts.hour
    return 0.35 + 0.65 * max(0.0, 1 - abs(h - 15) / 12)

def gen_web(ts_hour):
    """~840 peak/hour nginx combined logs."""
    n = int(400 * diurnal(ts_hour) * random.uniform(0.85, 1.15))
    if in_incident(ts_hour):
        n = int(n * 1.4)  # retries inflate traffic
    out = []
    for _ in range(n):
        ts = ts_hour + timedelta(seconds=random.uniform(0, 3600))
        uri = random.choices(URI_NAMES, URI_W)[0]
        if in_incident(ts) and uri in ("/api/payment", "/api/checkout"):
            status = random.choices([502, 503, 500, 200], [40, 25, 15, 20])[0]
        else:
            status = random.choices([200, 201, 301, 404, 401, 500],
                                    [82, 5, 3, 6, 3, 1])[0]
        rt = random.uniform(0.02, 0.4)
        if in_incident(ts) and uri.startswith("/api/"):
            rt = random.uniform(1.5, 9.0)
        size = random.randint(180, 24000) if status < 400 else random.randint(90, 600)
        method = "POST" if uri in ("/api/checkout", "/api/payment", "/api/login") else "GET"
        raw = (f'{rand_ip()} - {random.choice(USERS)} [{ts.strftime("%d/%b/%Y:%H:%M:%S +0000")}] '
               f'"{method} {uri} HTTP/1.1" {status} {size} "-" "{random.choice(AGENTS)}" {rt:.3f}')
        out.append({"time": ts.timestamp(), "host": random.choice(HOSTS_WEB),
                    "index": "web", "sourcetype": "access_combined",
                    "source": "/var/log/nginx/access.log", "event": raw})
    return out

ERRORS = {
    "payments": ["ConnectionPoolTimeout: no available connection to payments-db after 5000ms",
                 "StripeGatewayError: upstream returned 502",
                 "TransactionAbortedException: lock wait timeout on table `charges`"],
    "checkout": ["OrderValidationError: cart snapshot mismatch",
                 "DownstreamTimeout: payments did not respond in 3000ms"],
    "catalog": ["CacheMissStorm: redis latency 900ms"],
    "auth-svc": ["TokenSignatureExpired for session refresh"],
    "search": ["QueryParseError: unbalanced quotes in user query"],
    "cart": ["OptimisticLockException on cart update"],
}

def gen_app(ts_hour):
    """~550 peak/hour JSON service logs."""
    n = int(260 * diurnal(ts_hour) * random.uniform(0.85, 1.15))
    out = []
    for _ in range(n):
        ts = ts_hour + timedelta(seconds=random.uniform(0, 3600))
        svc = random.choice(SERVICES)
        if in_incident(ts) and svc in ("payments", "checkout"):
            level = random.choices(["ERROR", "WARN", "INFO"], [55, 25, 20])[0]
        else:
            level = random.choices(["INFO", "DEBUG", "WARN", "ERROR"], [70, 18, 9, 3])[0]
        latency = random.uniform(5, 180)
        if in_incident(ts) and svc in ("payments", "checkout"):
            latency = random.uniform(800, 6000)
        msg = (random.choice(ERRORS[svc]) if level == "ERROR"
               else f"handled {random.choices(URI_NAMES, URI_W)[0]} request")
        evt = {"level": level, "service": svc, "message": msg,
               "latency_ms": round(latency, 1), "user_id": random.choice(USERS),
               "trace_id": f"{random.getrandbits(64):016x}", "env": "production"}
        out.append({"time": ts.timestamp(), "host": random.choice(HOSTS_APP[svc]),
                    "index": "app", "sourcetype": "app_logs",
                    "source": f"/var/log/{svc}/service.log", "event": evt})
    return out

def gen_auth(ts_hour):
    """~280 peak/hour auth events + brute-force bursts from 3 attack IPs."""
    n = int(130 * diurnal(ts_hour) * random.uniform(0.85, 1.15))
    out = []
    for _ in range(n):
        ts = ts_hour + timedelta(seconds=random.uniform(0, 3600))
        ok = random.random() < 0.93
        user = random.choice(USERS)
        ip = rand_ip()
        action = "success" if ok else "failure"
        raw = (f'{ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]}Z action={action} user={user} '
               f'src_ip={ip} method={"password" if random.random()<0.7 else "sso"} app=webauth')
        out.append({"time": ts.timestamp(), "host": "auth-gw-01", "index": "security",
                    "sourcetype": "auth", "source": "/var/log/auth/webauth.log", "event": raw})
    # brute force: every hour a burst of failures from the attack IPs
    for ip in ATTACK_IPS:
        for _ in range(random.randint(15, 40)):
            ts = ts_hour + timedelta(seconds=random.uniform(0, 3600))
            user = random.choice(["admin", "root", "test", random.choice(USERS)])
            raw = (f'{ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]}Z action=failure user={user} '
                   f'src_ip={ip} method=password app=webauth reason=bad_password')
            out.append({"time": ts.timestamp(), "host": "auth-gw-01", "index": "security",
                        "sourcetype": "auth", "source": "/var/log/auth/webauth.log", "event": raw})
    return out

# ── ship it ──────────────────────────────────────────────────────────────────
sess = requests.Session()
sess.headers.update({"Authorization": f"Splunk {HEC_TOKEN}",
                     "Content-Encoding": "gzip"})

def flush(batch):
    payload = gzip.compress("\n".join(json.dumps(e) for e in batch).encode())
    for attempt in range(5):
        try:
            r = sess.post(HEC, data=payload, verify=False, timeout=60)
            if r.status_code == 200:
                return len(batch)
            print("HEC error", r.status_code, r.text[:200])
        except Exception as e:
            print("HEC exception", e)
        time.sleep(2 * (attempt + 1))
    raise SystemExit("HEC ingest failed repeatedly")

total = 0
batch = []
hours = int((now - START).total_seconds() // 3600)
for h in range(hours + 1):
    ts_hour = START + timedelta(hours=h)
    for gen in (gen_web, gen_app, gen_auth):
        batch.extend(gen(ts_hour))
        while len(batch) >= 2000:
            total += flush(batch[:2000]); batch = batch[2000:]
if batch:
    total += flush(batch)
print(f"SEEDED {total} events into Splunk (web/app/security), "
      f"incident window {INCIDENT_START.isoformat()} → {INCIDENT_END.isoformat()}")

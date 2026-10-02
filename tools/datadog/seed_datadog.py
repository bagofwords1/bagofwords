#!/usr/bin/env python3
"""Seed a Datadog org with a small, realistic observability dataset — agentless.

Exists so the planned `datadog` connector (see docs/design) has something to
discover and query in a trial org without running the Datadog Agent. Posts
straight to the public intake APIs with only an API key:

  * metrics  → POST /api/v2/series            (gauges + counts, last 60 min —
                                               the intake rejects points older
                                               than ~1h, so re-run to refresh)
  * logs     → POST http-intake.logs/api/v2/logs (last 6h, structured attrs)
  * events   → POST /api/v1/events            (deploy markers)

Four fake services under `env:bow-demo`. The data carries an RCA story: a
`payments` deploy ~25 min ago is followed by a latency spike and a burst of
`GatewayTimeout` errors, which also drags `checkout` error rates up.

APM spans are NOT seeded: agentless span intake (OTLP) is a preview that needs
allowlisting. Use a real Agent on a VM for `dd.spans` data.

Usage:
  DD_API_KEY=... DD_SITE=datadoghq.com python tools/datadog/seed_datadog.py
  (add --dry-run to print counts without sending)
"""
import argparse
import gzip
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

random.seed(1337)

ENV_TAG = "env:bow-demo"
SERVICES = {
    # service: (base p95 latency ms, base req/min, base error ratio)
    "web-frontend": (120, 900, 0.004),
    "checkout": (210, 300, 0.006),
    "payments": (180, 260, 0.003),
    "inventory": (60, 500, 0.002),
}
DEPLOY_MINUTES_AGO = 25
ENDPOINTS = {
    "web-frontend": ["GET /", "GET /product/{id}", "GET /cart"],
    "checkout": ["POST /checkout", "GET /checkout/{id}"],
    "payments": ["POST /charge", "POST /refund"],
    "inventory": ["GET /stock/{sku}", "POST /reserve"],
}
REGIONS = ["us-east-1", "eu-west-1"]


def _post(url: str, api_key: str, payload, gzip_body: bool = False) -> int:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "DD-API-KEY": api_key}
    if gzip_body:
        body = gzip.compress(body)
        headers["Content-Encoding"] = "gzip"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        sys.exit(f"{url} → HTTP {e.code}: {e.read().decode(errors='replace')[:500]}")


def _incident(service: str, minutes_ago: float) -> bool:
    return service in ("payments", "checkout") and minutes_ago <= DEPLOY_MINUTES_AGO - 2


def build_series(now: int) -> list:
    series = {}

    def add(metric, mtype, tags, ts, value):
        key = (metric, tuple(tags))
        if key not in series:
            series[key] = {"metric": metric, "type": mtype, "tags": tags, "points": []}
            if mtype == 1:  # count → needs an interval
                series[key]["interval"] = 60
        series[key]["points"].append({"timestamp": ts, "value": round(value, 3)})

    for m in range(59, -1, -1):
        ts = now - m * 60
        for svc, (p95, rpm, err) in SERVICES.items():
            for region in REGIONS:
                tags = [ENV_TAG, f"service:{svc}", f"region:{region}"]
                hot = _incident(svc, m)
                lat = p95 * (3.5 if hot and svc == "payments" else 1.6 if hot else 1.0)
                lat *= random.uniform(0.85, 1.15)
                reqs = rpm / len(REGIONS) * random.uniform(0.9, 1.1)
                ratio = err * (25 if hot and svc == "payments" else 8 if hot else 1)
                add("bow.demo.request.latency.p95", 0, tags, ts, lat)               # gauge
                add("bow.demo.request.hits", 1, tags, ts, reqs)                    # count
                add("bow.demo.request.errors", 1, tags, ts, reqs * ratio)          # count
                add("bow.demo.host.cpu.pct", 0, tags, ts,
                    min(99, random.uniform(25, 45) * (1.8 if hot else 1)))
    return list(series.values())


def build_logs(now: int, count: int) -> list:
    logs = []
    for _ in range(count):
        minutes_ago = random.uniform(0, 360)
        svc = random.choices(list(SERVICES), weights=[4, 2, 2, 3])[0]
        hot = _incident(svc, minutes_ago)
        base_err = SERVICES[svc][2] * 10
        is_err = random.random() < (0.45 if hot else base_err)
        endpoint = random.choice(ENDPOINTS[svc])
        method, route = endpoint.split(" ", 1)
        duration = SERVICES[svc][0] * random.uniform(0.3, 1.2) * (3.5 if hot else 1)
        entry = {
            "ddsource": "python",
            "ddtags": f"{ENV_TAG},region:{random.choice(REGIONS)}",
            "hostname": f"{svc}-{random.randint(1, 3)}",
            "service": svc,
            "status": "error" if is_err else random.choices(["info", "warn"], [9, 1])[0],
            "timestamp": int((now - minutes_ago * 60) * 1000),
            "http": {"method": method, "url_details": {"path": route},
                     "status_code": (504 if hot else random.choice([500, 502])) if is_err else 200},
            "duration_ms": round(duration, 1),
            "customer": {"tier": random.choice(["free", "pro", "enterprise"]),
                         "id": f"cus_{random.randint(1000, 1400)}"},
        }
        if is_err:
            kind = "GatewayTimeout" if hot else random.choice(["DBConnectionError", "ValidationError"])
            entry["error"] = {"kind": kind}
            entry["message"] = f"{endpoint} failed: {kind}"
        else:
            entry["message"] = f"{endpoint} {entry['http']['status_code']} in {entry['duration_ms']}ms"
        logs.append(entry)
    return logs


def build_events(now: int) -> list:
    def deploy(svc, minutes_ago, version):
        return {
            "title": f"Deployed {svc} {version}",
            "text": f"{svc} rolled out {version} to {ENV_TAG}",
            "date_happened": now - minutes_ago * 60,
            "tags": [ENV_TAG, f"service:{svc}", "source:bow-seed", f"version:{version}"],
            "alert_type": "info",
            "source_type_name": "deployment",
        }
    return [
        deploy("inventory", 300, "v3.8.0"),
        deploy("web-frontend", 140, "v12.4.1"),
        deploy("payments", DEPLOY_MINUTES_AGO, "v2.17.0"),
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", type=int, default=4000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    api_key = os.environ.get("DD_API_KEY")
    site = os.environ.get("DD_SITE", "datadoghq.com")
    if not api_key and not args.dry_run:
        sys.exit("DD_API_KEY is required")

    now = int(time.time())
    series, logs, events = build_series(now), build_logs(now, args.logs), build_events(now)
    print(f"series={len(series)} points={sum(len(s['points']) for s in series)} "
          f"logs={len(logs)} events={len(events)}")
    if args.dry_run:
        return

    for i in range(0, len(series), 50):
        _post(f"https://api.{site}/api/v2/series", api_key, {"series": series[i:i + 50]}, gzip_body=True)
    for i in range(0, len(logs), 500):
        _post(f"https://http-intake.logs.{site}/api/v2/logs", api_key, logs[i:i + 500], gzip_body=True)
    for ev in events:
        _post(f"https://api.{site}/api/v1/events", api_key, ev)
    print("seeded ok — metrics `bow.demo.*`, logs/events tagged", ENV_TAG)


if __name__ == "__main__":
    main()

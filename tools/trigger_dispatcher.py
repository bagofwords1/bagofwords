#!/usr/bin/env python3
"""Custom webhook dispatcher — simulates an external alerting system.

Sends signed event payloads to a trigger's delivery URL so the
trigger → spawn → agent pipeline can be exercised end-to-end without a real
alerting stack. Speaks the generic adapter's dialect
(app/services/webhook_adapters/generic_adapter.py):

- auth `token`     → `Authorization: Bearer <secret>` header
- auth `hmac`      → `X-BOW-Signature-256: sha256=hmac(secret, "{ts}.{body}")`
                     + `X-BOW-Timestamp` (replay guard)
- auth `url_token` → `?k=<secret>` query param
- dedup            → `X-BOW-Delivery: <delivery id>` (idempotency key)

Examples:
    # Fire the built-in alert payload
    python tools/trigger_dispatcher.py --url http://localhost:8000/webhooks/whk_xxx \
        --secret whsec_yyy --sample alert

    # Prove idempotency: same delivery id twice → one session
    python tools/trigger_dispatcher.py --url ... --secret ... --sample alert \
        --delivery-id dup-1 --count 2

    # HMAC-signed delivery
    python tools/trigger_dispatcher.py --url ... --secret ... --auth hmac --sample alert

    # Custom payload from a file
    python tools/trigger_dispatcher.py --url ... --secret ... --payload alert.json
"""
import argparse
import hashlib
import hmac
import json
import sys
import time
import urllib.parse
import urllib.request
import uuid

SAMPLES = {
    # A realistic monitoring alert — the RCA-style event a trigger is for.
    "alert": {
        "type": "alert",
        "title": "High error rate on checkout-service",
        "severity": "P1",
        "service": "checkout-service",
        "message": "HTTP 5xx rate exceeded 5% over the last 10 minutes (current: 12.3%)",
        "started_at": "2026-07-07T09:42:00Z",
        "metric": {"name": "http_server_errors_rate", "value": 0.123, "threshold": 0.05},
        "environment": "production",
    },
    # Noise: a heartbeat the classifier gate should decline.
    "heartbeat": {
        "type": "heartbeat",
        "title": "uptime check ok",
        "service": "checkout-service",
        "message": "all probes green",
    },
    # A resolved notification.
    "resolved": {
        "type": "alert_resolved",
        "title": "Error rate back to normal on checkout-service",
        "severity": "P1",
        "service": "checkout-service",
        "message": "HTTP 5xx rate back under threshold (current: 0.4%)",
    },
}


def build_request(url: str, secret: str, auth: str, body: bytes, delivery_id: str):
    headers = {
        "Content-Type": "application/json",
        "X-BOW-Delivery": delivery_id,
        "User-Agent": "trigger-dispatcher/1.0",
    }
    if auth == "token":
        headers["Authorization"] = f"Bearer {secret}"
    elif auth == "hmac":
        ts = str(int(time.time()))
        signed = f"{ts}.".encode() + body
        sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        headers["X-BOW-Timestamp"] = ts
        headers["X-BOW-Signature-256"] = f"sha256={sig}"
    elif auth == "url_token":
        sep = "&" if urllib.parse.urlparse(url).query else "?"
        url = f"{url}{sep}k={urllib.parse.quote(secret)}"
    else:
        raise SystemExit(f"unknown auth mode: {auth}")
    return urllib.request.Request(url, data=body, headers=headers, method="POST")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True, help="Trigger delivery URL (http://host/webhooks/whk_...)")
    p.add_argument("--secret", required=True, help="Trigger signing secret (whsec_...)")
    p.add_argument("--auth", default="token", choices=["token", "hmac", "url_token"],
                   help="Auth mode — must match the trigger's configuration (default: token)")
    p.add_argument("--sample", choices=sorted(SAMPLES), help="Built-in sample payload")
    p.add_argument("--payload", help="Path to a JSON payload file (overrides --sample)")
    p.add_argument("--delivery-id", default=None,
                   help="Idempotency key (X-BOW-Delivery). Default: random per send. "
                        "Fix it across --count sends to test dedup.")
    p.add_argument("--count", type=int, default=1, help="Number of sends (default 1)")
    p.add_argument("--interval", type=float, default=0.5, help="Seconds between sends")
    args = p.parse_args()

    if args.payload:
        with open(args.payload) as f:
            payload = json.load(f)
    elif args.sample:
        payload = SAMPLES[args.sample]
    else:
        p.error("one of --sample or --payload is required")

    body = json.dumps(payload).encode()
    failures = 0
    for i in range(args.count):
        delivery_id = args.delivery_id or f"disp-{uuid.uuid4().hex[:12]}"
        req = build_request(args.url, args.secret, args.auth, body, delivery_id)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode()
                print(f"[{i + 1}/{args.count}] {resp.status} {text} (delivery={delivery_id})")
        except urllib.error.HTTPError as e:
            failures += 1
            print(f"[{i + 1}/{args.count}] {e.code} {e.read().decode()} (delivery={delivery_id})",
                  file=sys.stderr)
        except Exception as e:
            failures += 1
            print(f"[{i + 1}/{args.count}] FAILED: {e}", file=sys.stderr)
        if i + 1 < args.count:
            time.sleep(args.interval)
    sys.exit(1 if failures == args.count else 0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Independent audit log collector used to verify BOW audit streams.

Behaves like a real SIEM intake, not like the application's own mock:
  * HTTPS webhook intake  POST /webhook        Authorization: Bearer <token>, HMAC (X-BOW-Timestamp/X-BOW-Signature)
  * Splunk HEC intake     POST /splunk/services/collector/event   Authorization: Splunk <token>, NDJSON
  * every delivery is logged (deliveries.jsonl) and every event is stored (events.jsonl)
  * events are deduplicated by envelope id (at-least-once delivery is expected; duplicates are counted, not dropped silently)
  * each event is validated against what a collector needs: id, version, action, occurred_at (UTC, parseable),
    organization.id, actor.type; failures are counted and listed
  * ingest latency = receipt time - occurred_at
  * fault injection: POST /_control/fault {"mode": "503|429|401|timeout|reset", "count": N}; DELETE /_control/fault
  * inspection: GET /_control/stats, GET /_control/events?action=<prefix>&since=<delivery_no>
"""
import hashlib
import hmac
import json
import os
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATE_DIR = sys.argv[1] if len(sys.argv) > 1 else "./collector-state"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8790
WEBHOOK_TOKEN = os.environ.get("COLLECTOR_TOKEN", "collector-bearer-token")
HMAC_SECRET = os.environ.get("COLLECTOR_HMAC", "collector-hmac-secret")
HEC_TOKEN = os.environ.get("COLLECTOR_HEC", "collector-hec-token")
os.makedirs(STATE_DIR, exist_ok=True)

LOCK = threading.Lock()
STATE = {
    "deliveries": 0,
    "accepted_deliveries": 0,
    "rejected_deliveries": 0,
    "events_received": 0,
    "unique_events": 0,
    "duplicates": 0,
    "invalid_events": 0,
    "invalid_samples": [],
    "latency_s": [],
    "faults": None,  # {"mode", "count"}
}
SEEN = {}   # id -> first delivery number
EVENTS = []  # list of {delivery, received_at, intake, event, dup, problems}
REQUIRED = ("id", "version", "action", "occurred_at")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def validate(e):
    problems = []
    if not isinstance(e, dict):
        return ["not an object"]
    for k in REQUIRED:
        if e.get(k) in (None, ""):
            problems.append(f"missing {k}")
    ts = parse_ts(e.get("occurred_at"))
    if ts is None:
        problems.append("occurred_at unparseable")
    elif ts.tzinfo is None:
        problems.append("occurred_at has no timezone")
    org = (e.get("organization") or {}).get("id")
    if not org:
        problems.append("missing organization.id")
    actor = e.get("actor") or {}
    if not actor.get("type"):
        problems.append("missing actor.type")
    if "outcome" not in e:
        problems.append("no outcome field")
    return problems


def record(intake, events, headers, delivery_no):
    received = datetime.now(timezone.utc)
    with LOCK:
        for e in events:
            STATE["events_received"] += 1
            problems = validate(e)
            eid = e.get("id") if isinstance(e, dict) else None
            dup = eid in SEEN
            if dup:
                STATE["duplicates"] += 1
            elif eid:
                SEEN[eid] = delivery_no
                STATE["unique_events"] += 1
            if problems:
                STATE["invalid_events"] += 1
                if len(STATE["invalid_samples"]) < 20:
                    STATE["invalid_samples"].append({"id": eid, "action": e.get("action") if isinstance(e, dict) else None, "problems": problems})
            ts = parse_ts(e.get("occurred_at")) if isinstance(e, dict) else None
            lat = (received - ts).total_seconds() if ts and ts.tzinfo else None
            if lat is not None:
                STATE["latency_s"].append(lat)
            rec = {"delivery": delivery_no, "received_at": received.isoformat(timespec="milliseconds"), "intake": intake,
                   "dup": dup, "problems": problems, "latency_s": lat, "event": e}
            EVENTS.append(rec)
            with open(os.path.join(STATE_DIR, "events.jsonl"), "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, status, body=b"", ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _log_delivery(self, intake, status, n_events, note, body_len):
        with LOCK:
            STATE["deliveries"] += 1
            no = STATE["deliveries"]
            if 200 <= status < 300:
                STATE["accepted_deliveries"] += 1
            else:
                STATE["rejected_deliveries"] += 1
        safe_headers = {k: ("<redacted>" if k.lower() in ("authorization", "x-bow-signature") else v) for k, v in self.headers.items()}
        with open(os.path.join(STATE_DIR, "deliveries.jsonl"), "a") as f:
            f.write(json.dumps({"delivery": no, "at": now_iso(), "intake": intake, "status": status, "events": n_events,
                                "bytes": body_len, "note": note, "headers": safe_headers}) + "\n")
        return no

    def _fault(self, intake, body_len):
        with LOCK:
            f = STATE["faults"]
            if not f or f["count"] <= 0:
                return False
            f["count"] -= 1
            mode = f["mode"]
            if f["count"] <= 0:
                STATE["faults"] = None
        self._log_delivery(intake, int(mode) if mode.isdigit() else 0, 0, f"fault:{mode}", body_len)
        if mode == "timeout":
            time.sleep(25)
            return True
        if mode == "reset":
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            self.connection.close()
            return True
        self._send(int(mode), {"error": f"injected {mode}"})
        return True

    # --- intakes -------------------------------------------------------
    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()
        if u.path == "/_control/fault":
            d = json.loads(body or b"{}")
            with LOCK:
                STATE["faults"] = {"mode": str(d["mode"]), "count": int(d.get("count", 1))}
            return self._send(200, {"ok": True, "faults": STATE["faults"]})
        if u.path == "/_control/reset":
            with LOCK:
                for k in ("deliveries", "accepted_deliveries", "rejected_deliveries", "events_received", "unique_events", "duplicates", "invalid_events"):
                    STATE[k] = 0
                STATE["invalid_samples"] = []
                STATE["latency_s"] = []
                STATE["faults"] = None
                SEEN.clear()
                EVENTS.clear()
            return self._send(200, {"ok": True})
        if u.path == "/webhook":
            return self._webhook(body)
        if u.path.startswith("/splunk/services/collector"):
            return self._hec(body)
        return self._send(404, {"error": "no such intake"})

    def _webhook(self, body):
        intake = "webhook"
        if self._fault(intake, len(body)):
            return
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {WEBHOOK_TOKEN}":
            self._log_delivery(intake, 401, 0, "bad bearer", len(body))
            return self._send(401, {"error": "unauthorized"})
        ts = self.headers.get("X-BOW-Timestamp", "")
        sig = self.headers.get("X-BOW-Signature", "")
        want = "v1=" + hmac.new(HMAC_SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        if not ts or not hmac.compare_digest(want, sig) or abs(time.time() - int(ts or 0)) > 300:
            self._log_delivery(intake, 401, 0, "bad hmac", len(body))
            return self._send(401, {"error": "bad signature"})
        try:
            events = json.loads(body)
            assert isinstance(events, list)
        except Exception:
            self._log_delivery(intake, 400, 0, "not a json array", len(body))
            return self._send(400, {"error": "expected json array"})
        no = self._log_delivery(intake, 200, len(events), "ok", len(body))
        record(intake, events, self.headers, no)
        return self._send(200, {"accepted": len(events)})

    def _hec(self, body):
        intake = "hec"
        if self._fault(intake, len(body)):
            return
        if self.headers.get("Authorization", "") != f"Splunk {HEC_TOKEN}":
            self._log_delivery(intake, 401, 0, "bad hec token", len(body))
            return self._send(401, {"text": "Invalid token", "code": 4})
        events = []
        try:
            for line in body.decode().splitlines():
                if line.strip():
                    item = json.loads(line)
                    ev = item.get("event")
                    if isinstance(ev, dict):
                        ev = dict(ev)
                        ev["_hec_time"] = item.get("time")
                        ev["_hec_sourcetype"] = item.get("sourcetype")
                    events.append(ev)
        except Exception as e:
            self._log_delivery(intake, 400, 0, f"bad ndjson: {e}", len(body))
            return self._send(400, {"text": "Invalid data format", "code": 6})
        no = self._log_delivery(intake, 200, len(events), "ok", len(body))
        record(intake, events, self.headers, no)
        return self._send(200, {"text": "Success", "code": 0})

    # --- inspection ----------------------------------------------------
    def do_DELETE(self):
        if urlparse(self.path).path == "/_control/fault":
            with LOCK:
                STATE["faults"] = None
            return self._send(200, {"ok": True})
        return self._send(404)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/_control/stats":
            with LOCK:
                lat = sorted(STATE["latency_s"])
                out = {k: v for k, v in STATE.items() if k != "latency_s"}
                out["latency"] = {"n": len(lat), "p50": lat[len(lat) // 2] if lat else None, "max": lat[-1] if lat else None, "min": lat[0] if lat else None}
                out["actions"] = {}
                for r in EVENTS:
                    if not r["dup"] and isinstance(r["event"], dict):
                        a = r["event"].get("action")
                        out["actions"][a] = out["actions"].get(a, 0) + 1
            return self._send(200, out)
        if u.path == "/_control/events":
            prefix = q.get("action", [""])[0]
            since = int(q.get("since", ["0"])[0])
            with LOCK:
                rows = [r for r in EVENTS if r["delivery"] > since and (not prefix or str((r["event"] or {}).get("action", "")).startswith(prefix))]
            return self._send(200, rows)
        return self._send(404)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"collector listening on http://127.0.0.1:{PORT} state={STATE_DIR}", flush=True)
    srv.serve_forever()

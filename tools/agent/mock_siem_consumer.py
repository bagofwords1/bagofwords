#!/usr/bin/env python3
"""Mock SIEM / object-store intake for verifying audit log streams WITHOUT any
vendor account.

It emulates the documented intake surface of each destination closely enough to
validate wire format and auth — and nothing more. It is NOT a vendor product.
It records every envelope it receives, injects faults on command, and computes
delivery stats (missing / duplicate / out-of-order) against an id list taken
from the database, which is what proves "no gaps" end to end.

HTTP (default port 8790):

    POST /dd/api/v2/logs                                    Datadog logs intake   (DD-API-KEY)
    POST /splunk/services/collector/event                   Splunk HEC            (Authorization: Splunk <token>)
    POST /entra/{tenant}/oauth2/v2.0/token                  Entra token endpoint  (client credentials)
    POST /sentinel/dataCollectionRules/{dcr}/streams/{s}    Logs Ingestion API    (Bearer from the token endpoint)
    PUT  /s3/{bucket}/{key}                                 S3 / GCS PutObject    (SigV4 header present, Content-MD5 checked)
    POST /sts                                               STS AssumeRole        (ExternalId checked)
    POST /https/{anything}                                  Generic HTTPS         (X-BOW-Signature HMAC checked)

TCP+TLS (default port 6514): syslog RFC 5424 with RFC 5425 octet-counting.
Plain TCP syslog on --syslog-plain-port when given.

Control / inspection:

    POST   /_control/fault   {"dest": "splunk", "mode": "503|429|500|401|403|400|reset|timeout", "count": 3, "sleep": 20}
    DELETE /_control/fault
    POST   /_control/config  {"dd_api_key": "...", "hec_token": "...", "hmac_secret": "...", "external_id": "...", ...}
    GET    /_received?dest=splunk
    POST   /_stats           {"dest": "splunk", "expect_ids": ["..."]}   (GET /_stats?dest= without ids)
    DELETE /_received
    GET    /_health

Default credentials: DD key ``demo-dd-key``, HEC token ``demo-hec-token``,
Entra ``demo-client``/``demo-secret``, S3/GCS access key ``demo-access-key``,
HMAC secret ``demo-hmac-secret``.

Run:  python tools/agent/mock_siem_consumer.py [--port 8790] [--syslog-port 6514] [--state-dir /tmp/siem-mock]
Import (tests): ``MockSiem(port=0).start()`` → ``.url``, ``.syslog_port``, ``.ca_pem``.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import ssl
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ENVELOPE_KEYS = {"id", "version", "action", "occurred_at", "organization", "actor", "targets", "context", "metadata"}
S3_KEY_RE = re.compile(r"^(?:.+/)?\d{4}-\d{2}-\d{2}/\d{8}T\d{9}Z_[0-9a-f-]{36}\.json(\.gz)?$")
RFC5424_RE = re.compile(r"^<(\d{1,3})>1 (\S+) (\S+) (\S+) (\S+) (\S+) (-|\[.*?\]) (.*)$", re.S)
TEST_ACTION = "audit_stream.test"

DEFAULT_CONFIG = {
    "dd_api_key": "demo-dd-key",
    "hec_token": "demo-hec-token",
    "client_id": "demo-client",
    "client_secret": "demo-secret",
    "s3_access_key": "demo-access-key",
    "hmac_secret": "demo-hmac-secret",
    "require_signature": True,
    "external_id": None,
}


def valid_envelope(e) -> bool:
    return isinstance(e, dict) and ENVELOPE_KEYS <= set(e) and e.get("version") == 1 and bool(e.get("id"))


class State:
    def __init__(self, state_dir: str | None):
        self.lock = threading.Lock()
        self.state_dir = state_dir
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)
        self.reset()
        self.config = dict(DEFAULT_CONFIG)
        self.faults: dict = {}

    def reset(self):
        self.received: dict[str, list] = {}
        self.s3_objects: dict[str, list] = {}
        self.auth_failures: dict[str, int] = {}
        self.format_errors: dict[str, int] = {}
        self.requests: dict[str, int] = {}
        self.tokens: set[str] = set()
        self.sts_keys: set[str] = set()

    def bump(self, table: dict, dest: str, n: int = 1):
        table[dest] = table.get(dest, 0) + n

    def take_fault(self, dest: str):
        with self.lock:
            f = self.faults.get(dest)
            if not f:
                return None
            if f["count"] > 0:
                f["count"] -= 1
                if f["count"] == 0:
                    self.faults.pop(dest, None)
            return dict(f)

    def record(self, dest: str, envelopes: list, raw_headers: dict | None = None):
        with self.lock:
            self.received.setdefault(dest, []).extend(envelopes)
        if self.state_dir:
            redacted = {k: ("<redacted>" if k.lower() in ("authorization", "dd-api-key", "x-bow-signature") else v)
                        for k, v in (raw_headers or {}).items()}
            with open(os.path.join(self.state_dir, f"{dest}.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": time.time(), "headers": redacted, "count": len(envelopes),
                                    "ids": [e.get("id") for e in envelopes]}) + "\n")

    def envelopes(self, dest: str) -> list:
        with self.lock:
            if dest in ("s3", "gcs"):
                return [e for (d, _k), evs in self.s3_objects.items() if d == dest for e in evs]
            return list(self.received.get(dest, []))

    def stats(self, dest: str, expect_ids=None) -> dict:
        evs = [e for e in self.envelopes(dest) if e.get("action") != TEST_ACTION]
        ids = [e.get("id") for e in evs]
        out_of_order = sum(1 for a, b in zip(evs, evs[1:]) if (b.get("occurred_at") or "") < (a.get("occurred_at") or ""))
        res = {
            "dest": dest,
            "received": len(ids),
            "unique": len(set(ids)),
            "duplicates": len(ids) - len(set(ids)),
            "out_of_order": out_of_order,
            "auth_failures": self.auth_failures.get(dest, 0),
            "format_errors": self.format_errors.get(dest, 0),
            "requests": self.requests.get(dest, 0),
            "test_events": len([e for e in self.envelopes(dest) if e.get("action") == TEST_ACTION]),
        }
        if expect_ids is not None:
            exp = set(expect_ids)
            got = set(ids)
            res["missing"] = sorted(exp - got)
            res["unexpected"] = sorted(got - exp)
        return res


def make_tls_material(directory: str) -> tuple[str, str, str]:
    """Self-signed cert for localhost/127.0.0.1; returns (cert, key, pem_text)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import ipaddress

    os.makedirs(directory, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    cp, kp = os.path.join(directory, "cert.pem"), os.path.join(directory, "key.pem")
    with open(cp, "w") as f:
        f.write(pem)
    with open(kp, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    return cp, kp, pem


def parse_syslog_frames(buf: bytes) -> tuple[list[bytes], bytes]:
    """Split octet-counted frames; return (complete_frames, remainder)."""
    frames = []
    while True:
        sp = buf.find(b" ")
        if sp <= 0 or not buf[:sp].isdigit():
            return frames, buf
        n = int(buf[:sp])
        if len(buf) < sp + 1 + n:
            return frames, buf
        frames.append(buf[sp + 1: sp + 1 + n])
        buf = buf[sp + 1 + n:]


def envelope_from_syslog(line: str):
    m = RFC5424_RE.match(line)
    if not m:
        return None
    msg = m.group(8)
    if msg.startswith("CEF:0|"):
        ext = dict(re.findall(r"(\w+)=((?:\\.|[^\\])*?)(?= \w+=|$)", msg.split("|", 7)[7]))
        return {"id": ext.get("externalId"), "version": 1, "action": msg.split("|")[4], "occurred_at": ext.get("rt"),
                "organization": {}, "actor": {}, "targets": [], "context": {}, "metadata": {}, "_format": "cef"}
    try:
        return json.loads(msg)
    except ValueError:
        return None


class Handler(BaseHTTPRequestHandler):
    state: State = None  # set per server
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        if os.environ.get("SIEM_MOCK_VERBOSE"):
            super().log_message(fmt, *args)

    # -- helpers --------------------------------------------------------------
    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, status: int, body=b"", ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _fault(self, dest: str) -> bool:
        """Apply an injected fault; True when the request was consumed."""
        f = self.state.take_fault(dest)
        if not f:
            return False
        mode = str(f["mode"])
        if mode == "reset":
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return True
        if mode == "timeout":
            time.sleep(float(f.get("sleep") or 20))
            self._send(504, {"error": "injected timeout"})
            return True
        status = int(mode)
        if dest in ("s3", "gcs"):
            code = {400: "InvalidRequest", 401: "AccessDenied", 403: "AccessDenied", 404: "NoSuchBucket",
                    429: "SlowDown", 500: "InternalError", 503: "SlowDown"}.get(status, "InternalError")
            self._send(status, f"<Error><Code>{code}</Code><Message>injected</Message></Error>", "application/xml")
        elif dest == "splunk" and status in (401, 403):
            self._send(status, {"text": "Invalid token", "code": 4})
        else:
            self._send(status, {"error": f"injected {status}"}, extra={"Retry-After": "1"} if status == 429 else None)
        return True

    def _auth_fail(self, dest: str, status=401, body=None):
        with self.state.lock:
            self.state.bump(self.state.auth_failures, dest)
        self._send(status, body or {"error": "unauthorized"})

    def _format_fail(self, dest: str, why: str):
        with self.state.lock:
            self.state.bump(self.state.format_errors, dest)
        self._send(400, {"error": f"format: {why}"})

    def _count(self, dest: str):
        with self.state.lock:
            self.state.bump(self.state.requests, dest)

    # -- routing --------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/_health":
            return self._send(200, {"ok": True})
        if u.path == "/_received":
            dest = (q.get("dest") or [""])[0]
            return self._send(200, self.state.envelopes(dest))
        if u.path == "/_stats":
            dest = (q.get("dest") or [""])[0]
            return self._send(200, self.state.stats(dest))
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        u = urlparse(self.path)
        if u.path == "/_control/fault":
            with self.state.lock:
                self.state.faults.clear()
            return self._send(200, {"ok": True})
        if u.path == "/_received":
            with self.state.lock:
                self.state.reset()
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def do_PUT(self):
        u = urlparse(self.path)
        if u.path.startswith("/s3/"):
            return self._s3_put(u)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        if p == "/_control/fault":
            d = json.loads(self._body() or b"{}")
            with self.state.lock:
                self.state.faults[d["dest"]] = {"mode": d["mode"], "count": int(d.get("count", 1)), "sleep": d.get("sleep")}
            return self._send(200, {"ok": True})
        if p == "/_control/config":
            d = json.loads(self._body() or b"{}")
            with self.state.lock:
                self.state.config.update(d)
            return self._send(200, self.state.config)
        if p == "/_stats":
            d = json.loads(self._body() or b"{}")
            return self._send(200, self.state.stats(d.get("dest", ""), d.get("expect_ids")))
        if p == "/dd/api/v2/logs":
            return self._datadog()
        if p.startswith("/splunk/services/collector"):
            return self._splunk()
        if p.startswith("/entra/") and p.endswith("/oauth2/v2.0/token"):
            return self._entra_token()
        if p.startswith("/sentinel/dataCollectionRules/"):
            return self._sentinel(u)
        if p.rstrip("/") == "/sts":
            return self._sts()
        if p.startswith("/https/"):
            return self._https()
        self._send(404, {"error": "not found"})

    # -- intakes --------------------------------------------------------------
    def _datadog(self):
        dest = "datadog"
        self._count(dest)
        body = self._body()
        if self._fault(dest):
            return
        if self.headers.get("DD-API-KEY") != self.state.config["dd_api_key"]:
            return self._auth_fail(dest, 403, {"errors": ["Forbidden"]})
        try:
            entries = json.loads(body)
            envs = [json.loads(x["message"]) for x in entries]
        except Exception as e:
            return self._format_fail(dest, f"not a Datadog log array: {e}")
        if not all(x.get("ddsource") for x in entries) or not all(valid_envelope(e) for e in envs):
            return self._format_fail(dest, "entry missing ddsource or envelope v1 fields")
        self.state.record(dest, envs, dict(self.headers))
        self._send(202, {})

    def _splunk(self):
        dest = "splunk"
        self._count(dest)
        body = self._body()
        if self._fault(dest):
            return
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Splunk "):
            return self._auth_fail(dest, 401, {"text": "Token is required", "code": 2})
        if auth.split(" ", 1)[1] != self.state.config["hec_token"]:
            return self._auth_fail(dest, 403, {"text": "Invalid token", "code": 4})
        try:
            items = [json.loads(line) for line in body.decode().splitlines() if line.strip()]
            envs = [i["event"] for i in items]
        except Exception as e:
            return self._format_fail(dest, f"not HEC NDJSON: {e}")
        if not all(valid_envelope(e) for e in envs) or not all("time" in i for i in items):
            return self._format_fail(dest, "HEC event missing time or envelope v1 fields")
        self.state.record(dest, envs, dict(self.headers))
        self._send(200, {"text": "Success", "code": 0})

    def _entra_token(self):
        dest = "sentinel"
        form = {k: v[0] for k, v in parse_qs(self._body().decode()).items()}
        cfg = self.state.config
        if (form.get("grant_type") != "client_credentials" or form.get("client_id") != cfg["client_id"]
                or form.get("client_secret") != cfg["client_secret"] or "monitor.azure.com" not in form.get("scope", "")):
            return self._auth_fail(dest, 401, {"error": "invalid_client"})
        tok = secrets.token_urlsafe(24)
        with self.state.lock:
            self.state.tokens.add(tok)
        self._send(200, {"token_type": "Bearer", "expires_in": 3600, "access_token": tok})

    def _sentinel(self, u):
        dest = "sentinel"
        self._count(dest)
        body = self._body()
        if self._fault(dest):
            return
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth.split(" ", 1)[1] not in self.state.tokens:
            return self._auth_fail(dest, 401, {"error": {"code": "InvalidToken"}})
        if "api-version" not in parse_qs(u.query):
            return self._format_fail(dest, "api-version query parameter missing")
        try:
            records = json.loads(body)
        except Exception as e:
            return self._format_fail(dest, f"not JSON: {e}")
        if not all("TimeGenerated" in r for r in records):
            return self._format_fail(dest, "record missing TimeGenerated")
        envs = [{k: v for k, v in r.items() if k != "TimeGenerated"} for r in records]
        if not all(valid_envelope(e) for e in envs):
            return self._format_fail(dest, "record missing envelope v1 fields")
        self.state.record(dest, envs, dict(self.headers))
        self._send(204)

    def _sts(self):
        form = {k: v[0] for k, v in parse_qs(self._body().decode()).items()}
        expected = self.state.config.get("external_id")
        if form.get("Action") != "AssumeRole" or (expected and form.get("ExternalId") != expected):
            with self.state.lock:
                self.state.bump(self.state.auth_failures, "s3")
            err = ('<ErrorResponse><Error><Type>Sender</Type><Code>AccessDenied</Code>'
                   '<Message>external id mismatch</Message></Error></ErrorResponse>')
            return self._send(403, err, "text/xml")
        ak = "ASIAMOCK" + secrets.token_hex(6).upper()
        with self.state.lock:
            self.state.sts_keys.add(ak)
        exp = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        xml = (
            '<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><AssumeRoleResult>'
            f'<Credentials><AccessKeyId>{ak}</AccessKeyId><SecretAccessKey>{secrets.token_hex(20)}</SecretAccessKey>'
            f'<SessionToken>{secrets.token_hex(32)}</SessionToken><Expiration>{exp}</Expiration></Credentials>'
            f'<AssumedRoleUser><Arn>{form.get("RoleArn")}/bagofwords</Arn><AssumedRoleId>AROAMOCK:bagofwords</AssumedRoleId></AssumedRoleUser>'
            '</AssumeRoleResult><ResponseMetadata><RequestId>mock</RequestId></ResponseMetadata></AssumeRoleResponse>'
        )
        self._send(200, xml, "text/xml")

    def _s3_put(self, u):
        parts = u.path.split("/", 3)  # ['', 's3', bucket, key]
        bucket, key = (parts[2], parts[3]) if len(parts) == 4 else (parts[-1], "")
        dest = "gcs" if bucket.startswith("gcs-") else "s3"
        self._count(dest)
        body = self._body()
        if self.headers.get("Expect", "").lower() == "100-continue":
            pass  # body already read; http.server sends no interim response
        if self._fault(dest):
            return
        auth = self.headers.get("Authorization", "")
        m = re.search(r"Credential=([^/]+)/", auth)
        ak = m.group(1) if m else None
        if not auth.startswith("AWS4-HMAC-SHA256") or (ak != self.state.config["s3_access_key"] and ak not in self.state.sts_keys):
            with self.state.lock:
                self.state.bump(self.state.auth_failures, dest)
            err = "<Error><Code>InvalidAccessKeyId</Code><Message>unknown key</Message></Error>"
            return self._send(403, err, "application/xml")
        md5 = self.headers.get("Content-MD5")
        if not md5 or base64.b64encode(hashlib.md5(body).digest()).decode() != md5:
            return self._format_fail(dest, "Content-MD5 missing or wrong (Object Lock buckets require it)")
        if not S3_KEY_RE.match(key):
            return self._format_fail(dest, f"object key does not match the date/ts_id layout: {key}")
        raw = gzip.decompress(body) if key.endswith(".gz") else body
        try:
            envs = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
        except Exception as e:
            return self._format_fail(dest, f"not NDJSON: {e}")
        if not all(valid_envelope(e) for e in envs):
            return self._format_fail(dest, "line missing envelope v1 fields")
        with self.state.lock:
            self.state.s3_objects[(dest, f"{bucket}/{key}")] = envs  # overwrite: same key == same batch
        self.state.record(dest, envs, dict(self.headers))
        etag = '"' + hashlib.md5(body).hexdigest() + '"'
        self._send(200, b"", "application/xml", extra={"ETag": etag})

    def _https(self):
        dest = "https"
        self._count(dest)
        body = self._body()
        if self._fault(dest):
            return
        cfg = self.state.config
        sig = self.headers.get("X-BOW-Signature")
        ts = self.headers.get("X-BOW-Timestamp")
        if cfg.get("require_signature"):
            if not sig or not ts:
                return self._auth_fail(dest, 401, {"error": "signature required"})
            want = "v1=" + hmac.new(cfg["hmac_secret"].encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(want, sig) or abs(time.time() - int(ts)) > 300:
                return self._auth_fail(dest, 401, {"error": "bad signature"})
        try:
            envs = json.loads(body)
        except Exception as e:
            return self._format_fail(dest, f"not JSON: {e}")
        if not isinstance(envs, list) or not all(valid_envelope(e) for e in envs):
            return self._format_fail(dest, "body is not an array of envelope v1")
        self.state.record(dest, envs, dict(self.headers))
        self._send(200, {"accepted": len(envs)})


class MockSiem:
    def __init__(self, port: int = 0, syslog_port: int | None = 0, syslog_plain_port: int | None = None,
                 host: str = "127.0.0.1", state_dir: str | None = None):
        self.host = host
        self.state = State(state_dir)
        handler = type("BoundHandler", (Handler,), {"state": self.state})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._threads: list[threading.Thread] = []
        self._socks: list[socket.socket] = []
        self.syslog_port = None
        self.syslog_plain_port = None
        self.ca_pem = None
        tls_dir = os.path.join(state_dir or tempfile.mkdtemp(prefix="siem-mock-"), "tls")
        if syslog_port is not None:
            cert, key, self.ca_pem = make_tls_material(tls_dir)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            self.syslog_port = self._listen(syslog_port, ctx)
        if syslog_plain_port is not None:
            self.syslog_plain_port = self._listen(syslog_plain_port, None)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _listen(self, port: int, ctx) -> int:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, port))
        s.listen(16)
        self._socks.append(s)
        t = threading.Thread(target=self._accept_loop, args=(s, ctx), daemon=True)
        self._threads.append(t)
        return s.getsockname()[1]

    def _accept_loop(self, s: socket.socket, ctx):
        while True:
            try:
                conn, _ = s.accept()
            except OSError:
                return
            threading.Thread(target=self._syslog_conn, args=(conn, ctx), daemon=True).start()

    def _syslog_conn(self, conn: socket.socket, ctx):
        dest = "syslog"
        try:
            if ctx is not None:
                conn = ctx.wrap_socket(conn, server_side=True)
            fault = self.state.take_fault(dest)
            if fault and fault["mode"] == "reset":
                conn.close()
                return
            buf = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                frames, buf = parse_syslog_frames(buf)
                envs = []
                for fr in frames:
                    env = envelope_from_syslog(fr.decode("utf-8", "replace"))
                    if env is None or not env.get("id"):
                        with self.state.lock:
                            self.state.bump(self.state.format_errors, dest)
                    else:
                        envs.append(env)
                if envs:
                    with self.state.lock:
                        self.state.bump(self.state.requests, dest)
                    self.state.record(dest, envs)
            if buf.strip():
                with self.state.lock:
                    self.state.bump(self.state.format_errors, dest)
        except (OSError, ssl.SSLError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def start(self) -> "MockSiem":
        t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        t.start()
        self._threads.append(t)
        for t in self._threads:
            if not t.is_alive():
                t.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for s in self._socks:
            try:
                s.close()
            except OSError:
                pass


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--syslog-port", type=int, default=6514)
    p.add_argument("--syslog-plain-port", type=int, default=None)
    p.add_argument("--state-dir", default=None)
    a = p.parse_args()
    m = MockSiem(a.port, a.syslog_port, a.syslog_plain_port, a.host, a.state_dir).start()
    print(json.dumps({"url": m.url, "syslog_tls_port": m.syslog_port, "syslog_plain_port": m.syslog_plain_port,
                      "ca_cert": os.path.join(a.state_dir, "tls", "cert.pem") if a.state_dir else None}), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        m.stop()


if __name__ == "__main__":
    main()

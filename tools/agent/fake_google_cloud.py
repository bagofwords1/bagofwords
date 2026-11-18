#!/usr/bin/env python3
"""A local stand-in for the three Google endpoints keyless BigQuery auth talks to.

Lets the *unmodified* backend, google-auth and google-cloud-bigquery run the
whole Workload Identity Federation flow with no Google account:

    subject token file --STS exchange--> federated token
        [--IAM generateAccessToken--> service-account token]   (impersonation)
            --> BigQuery REST (datasets, jobs, query results, table rows)

It answers as ``sts.googleapis.com``, ``iamcredentials.googleapis.com``,
``bigquery.googleapis.com`` and ``oauth2.googleapis.com`` over TLS on
127.0.0.1:443, with a certificate from a throwaway CA written next to the
generated files. The last host is the classic key flow: ``<run-dir>/legacy-key.json``
is a throwaway service-account key that works against this server only, for
testing a switch between a pasted key and keyless auth. Nothing in the app is
patched: the hostnames are pointed here through /etc/hosts, and the backend is
told to trust the throwaway CA and to skip the egress proxy for those hosts.

What it enforces, the way Google would:

  * OAuth2: the assertion must be signed by the legacy key generated here.
  * STS: the subject token must be the one in the token file, and the audience
    the configured workload identity provider.
  * IAM: only the federated principal may mint tokens, and only for service
    accounts in ``--impersonable`` (the "Service Account Token Creator" grant).
    Anything else is a 403.
  * BigQuery: every call needs a token minted above; principals not in
    ``--bigquery-principals`` get a 403. Each call is appended to
    ``<run-dir>/bigquery_audit.jsonl`` with the principal it ran as — that file
    is the proof of *which identity* a connection used.

SQL runs on an in-memory DuckDB holding one dataset (``sales``) with two small
tables. INFORMATION_SCHEMA catalog queries are answered from it.

Usage (root is needed for port 443 and /etc/hosts):

    cd backend
    uv run python ../tools/agent/fake_google_cloud.py --run-dir /tmp/fake-google --write-hosts

It prints the environment to start the backend with. Stop with Ctrl-C; remove
the /etc/hosts line it added with ``--remove-hosts``.

See docs/feedback-loops/bigquery-keyless-auth.md.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import ipaddress
import json
import os
import re
import ssl
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

HOSTS = ("sts.googleapis.com", "iamcredentials.googleapis.com", "bigquery.googleapis.com", "oauth2.googleapis.com")
HOSTS_MARK = "# fake_google_cloud.py"
PROJECT = "acme-analytics"
PROJECT_NUMBER = "123456789012"
POOL, PROVIDER = "onprem-pool", "compose-host"
AUDIENCE = (
    f"//iam.googleapis.com/projects/{PROJECT_NUMBER}/locations/global/"
    f"workloadIdentityPools/{POOL}/providers/{PROVIDER}"
)
SUBJECT = "system:serviceaccount:bow:backend"
FEDERATED_PRINCIPAL = (
    f"principal://iam.googleapis.com/projects/{PROJECT_NUMBER}/locations/global/"
    f"workloadIdentityPools/{POOL}/subject/{SUBJECT}"
)
DEFAULT_IMPERSONABLE = f"bq-reader@{PROJECT}.iam.gserviceaccount.com"
# The long-lived key a deployment would be migrating away from.
LEGACY_KEY_ACCOUNT = f"legacy-key@{PROJECT}.iam.gserviceaccount.com"

_lock = threading.Lock()
_tokens: dict[str, str] = {}      # access token -> principal
_jobs: dict[str, dict] = {}       # job id -> finished job
_state: dict = {}


# ── files the deployment would hold ─────────────────────────────────────────

def write_tls(run_dir: Path) -> tuple[Path, Path, Path]:
    """A throwaway CA and a server certificate for the three hostnames."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    now = dt.datetime.now(dt.UTC)

    def _key():
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    ca_key = _key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-google-cloud test CA")])
    ca_cert = (
        x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=7))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
            key_encipherment=False, data_encipherment=False, key_agreement=False,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = _key()
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOSTS[0])]))
        .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=7))
        .add_extension(x509.SubjectAlternativeName(
            [x509.DNSName(h) for h in HOSTS] + [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_path, cert_path, key_path = run_dir / "fake-ca.pem", run_dir / "server.pem", run_dir / "server.key"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    key_path.chmod(0o600)
    return ca_path, cert_path, key_path


def write_trust_bundle(run_dir: Path, ca_path: Path) -> Path:
    """The CA bundle the backend already trusts, plus the throwaway CA."""
    base = ""
    for var in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        p = os.environ.get(var)
        if p and os.path.isfile(p):
            base = Path(p).read_text()
            break
    else:
        try:
            import certifi
            base = Path(certifi.where()).read_text()
        except Exception:
            base = ""
    bundle = run_dir / "ca-bundle.pem"
    bundle.write_text(base.rstrip() + "\n" + ca_path.read_text())
    return bundle


def write_federation_config(run_dir: Path, subject_token: str) -> Path:
    """What `gcloud iam workload-identity-pools create-cred-config` produces for
    a file-sourced OIDC token. It holds no secret: only where to find the
    deployment's own token and which provider to present it to."""
    token_path = run_dir / "subject-token.jwt"
    token_path.write_text(subject_token)
    config = {
        "type": "external_account",
        "audience": AUDIENCE,
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "token_url": "https://sts.googleapis.com/v1/token",
        "credential_source": {"file": str(token_path)},
    }
    path = run_dir / "federation.json"
    path.write_text(json.dumps(config, indent=2))
    return path


def write_legacy_key(run_dir: Path) -> Path:
    """A service-account key file of the usual shape. The private key is
    generated here and is accepted by this server only."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    path = run_dir / "legacy-key.json"
    path.write_text(json.dumps({
        "type": "service_account", "project_id": PROJECT, "private_key_id": uuid.uuid4().hex,
        "private_key": pem, "client_email": LEGACY_KEY_ACCOUNT, "client_id": "100000000000000000001",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }, indent=2))
    path.chmod(0o600)
    _state["legacy_public_key"] = key.public_key()
    return path


def edit_hosts(add: bool) -> None:
    path = Path("/etc/hosts")
    kept = [line for line in path.read_text().splitlines() if HOSTS_MARK not in line]
    if add:
        kept.append(f"127.0.0.1 {' '.join(HOSTS)} {HOSTS_MARK}")
    path.write_text("\n".join(kept) + "\n")


# ── the warehouse ───────────────────────────────────────────────────────────

def build_warehouse():
    import duckdb

    db = duckdb.connect(":memory:")
    db.execute("CREATE TABLE customers (customer_id BIGINT, name VARCHAR, country VARCHAR)")
    db.execute("CREATE TABLE orders (order_id BIGINT, customer_id BIGINT, order_date DATE, amount DOUBLE, status VARCHAR)")
    db.executemany("INSERT INTO customers VALUES (?, ?, ?)", [
        (1, "Northwind Traders", "US"), (2, "Alpine Bikes", "CH"), (3, "Sakura Foods", "JP"), (4, "Baltic Timber", "LV"),
    ])
    db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", [
        (1001, 1, "2026-09-03", 1250.00, "shipped"), (1002, 1, "2026-09-17", 310.50, "shipped"),
        (1003, 2, "2026-10-01", 4999.99, "shipped"), (1004, 3, "2026-10-08", 87.25, "cancelled"),
        (1005, 3, "2026-10-21", 640.00, "shipped"), (1006, 4, "2026-11-02", 2100.00, "pending"),
        (1007, 2, "2026-11-05", 75.00, "shipped"), (1008, 1, "2026-11-11", 930.10, "pending"),
    ])
    return db


_BQ_TYPES = [("BIGINT", "INTEGER"), ("INTEGER", "INTEGER"), ("HUGEINT", "NUMERIC"), ("DOUBLE", "FLOAT"),
             ("FLOAT", "FLOAT"), ("DECIMAL", "NUMERIC"), ("BOOLEAN", "BOOLEAN"), ("DATE", "DATE"),
             ("TIMESTAMP", "TIMESTAMP"), ("VARCHAR", "STRING")]
_STANDARD_SQL = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL"}


def _bq_type(duck_type: str) -> str:
    t = str(duck_type).upper()
    for prefix, bq in _BQ_TYPES:
        if t.startswith(prefix):
            return bq
    return "STRING"


def _cell(value, bq_type: str):
    if value is None:
        return None
    if bq_type == "TIMESTAMP":
        return str(int(value.replace(tzinfo=value.tzinfo or dt.UTC).timestamp() * 1_000_000))
    if bq_type == "BOOLEAN":
        return "true" if value else "false"
    return str(value)


def _columns(db, table: str) -> list[tuple[str, str]]:
    return [(r[0], _bq_type(r[1])) for r in db.execute(f'DESCRIBE "{table}"').fetchall()]


def _tables(db) -> list[str]:
    return sorted(r[0] for r in db.execute("SHOW TABLES").fetchall())


def run_sql(sql: str) -> tuple[list[dict], list[list]]:
    """Execute BigQuery-flavoured SQL. Returns (schema fields, rows)."""
    db = _state["db"].cursor()
    if re.search(r"INFORMATION_SCHEMA\.COLUMNS", sql, re.I):
        rows = []
        for table in _tables(db):
            for name, bq in _columns(db, table):
                rows.append([table, name, _STANDARD_SQL.get(bq, bq), None, None])
        if re.search(r"TABLE_OPTIONS", sql, re.I):
            fields = ["table_name", "column_name", "data_type", "column_description", "table_description"]
        else:
            fields, rows = ["table_name", "column_name", "data_type"], [r[:3] for r in rows]
        return [{"name": f, "type": "STRING", "mode": "NULLABLE"} for f in fields], rows

    # `project.dataset.table`, `dataset.table` and backticked parts -> bare table.
    translated = sql.replace("`", "")
    translated = re.sub(rf"\b(?:{re.escape(PROJECT)}\.)?sales\.(\w+)", r"\1", translated)
    cur = db.execute(translated)
    schema = [{"name": d[0], "type": _bq_type(d[1]), "mode": "NULLABLE"} for d in cur.description]
    rows = [[_cell(v, schema[i]["type"]) for i, v in enumerate(row)] for row in cur.fetchall()]
    return schema, rows


# ── HTTP ────────────────────────────────────────────────────────────────────

def _audit(kind: str, **fields) -> None:
    entry = {"at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "kind": kind, **fields}
    with _lock, open(_state["audit"], "a") as f:
        f.write(json.dumps(entry) + "\n")


def _mint(principal: str, prefix: str) -> str:
    token = f"{prefix}.{uuid.uuid4().hex}"
    with _lock:
        _tokens[token] = principal
    return token


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):  # the audit file is the log
        pass

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, status: int, message: str, reason: str = "forbidden") -> None:
        self._send(status, {"error": {"code": status, "message": message, "status": reason.upper(),
                                      "errors": [{"message": message, "reason": reason}]}})

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _principal(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        return _tokens.get(auth.removeprefix("Bearer ").strip())

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method: str) -> None:
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        url = urlsplit(self.path)
        path, query = unquote(url.path), parse_qs(url.query)
        try:
            if host == "sts.googleapis.com":
                return self._sts(method, path)
            if host == "iamcredentials.googleapis.com":
                return self._iam(method, path)
            if host == "bigquery.googleapis.com":
                return self._bigquery(method, path, query)
            if host == "oauth2.googleapis.com":
                return self._oauth2(method, path)
            self._error(404, f"unknown host {host}", "notFound")
        except Exception as e:  # a bug here must look like a server error, not hang the client
            self._error(500, f"fake_google_cloud: {type(e).__name__}: {e}", "backendError")

    # -- Security Token Service: subject token -> federated token --
    def _sts(self, method: str, path: str) -> None:
        if method != "POST" or path != "/v1/token":
            return self._error(404, "not found", "notFound")
        raw = self._body()
        if "json" in (self.headers.get("Content-Type") or ""):
            form = {k: str(v) for k, v in json.loads(raw or b"{}").items()}
        else:
            form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
        expected = Path(_state["subject_token_path"]).read_text().strip()
        problem = None
        if form.get("grant_type") != "urn:ietf:params:oauth:grant-type:token-exchange":
            problem = "unsupported grant_type"
        elif form.get("audience") != AUDIENCE:
            problem = "The audience does not match a workload identity provider"
        elif (form.get("subject_token") or "").strip() != expected:
            problem = "The subject token is not valid for this provider"
        _audit("sts.token", ok=problem is None, audience=form.get("audience"), scope=form.get("scope"), problem=problem)
        if problem:
            return self._send(400, {"error": "invalid_grant", "error_description": problem})
        self._send(200, {
            "access_token": _mint(FEDERATED_PRINCIPAL, "federated"),
            "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "token_type": "Bearer", "expires_in": 3600,
        })

    # -- OAuth2 token endpoint: a JWT signed with a service-account key -> token --
    def _oauth2(self, method: str, path: str) -> None:
        if method != "POST" or path != "/token":
            return self._error(404, "not found", "notFound")
        import jwt

        form = {k: v[0] for k, v in parse_qs(self._body().decode()).items()}
        try:
            claims = jwt.decode(form.get("assertion") or "", _state["legacy_public_key"], algorithms=["RS256"],
                                audience="https://oauth2.googleapis.com/token")
            issuer = claims["iss"]
        except Exception as e:
            _audit("oauth2.token", ok=False, problem=f"{type(e).__name__}: {e}")
            return self._send(400, {"error": "invalid_grant", "error_description": "Invalid JWT Signature."})
        _audit("oauth2.token", ok=True, key_of=issuer)
        self._send(200, {"access_token": _mint(f"serviceAccount:{issuer}", "key"), "token_type": "Bearer", "expires_in": 3599})

    # -- IAM Credentials: federated token -> service-account token --
    def _iam(self, method: str, path: str) -> None:
        # Regional access boundary lookup google-auth makes for a new identity:
        # "no restriction".
        if method == "GET" and path.endswith("/allowedLocations"):
            return self._send(200, {"locations": [], "encodedLocations": "0x0"})
        m = re.fullmatch(r"/v1/projects/-/serviceAccounts/([^/:]+):generateAccessToken", path)
        if method != "POST" or not m:
            return self._error(404, "not found", "notFound")
        target, caller = m.group(1), self._principal()
        self._body()
        allowed = caller == FEDERATED_PRINCIPAL and target in _state["impersonable"]
        _audit("iam.generateAccessToken", ok=allowed, caller=caller, target=target)
        if caller is None:
            return self._error(401, "Request had invalid authentication credentials.", "unauthenticated")
        if not allowed:
            return self._error(
                403, f"Permission 'iam.serviceAccounts.getAccessToken' denied on resource {target} (or it may not exist).",
                "permission_denied")
        expires = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
        self._send(200, {"accessToken": _mint(f"serviceAccount:{target}", "sa"),
                         "expireTime": expires.strftime("%Y-%m-%dT%H:%M:%SZ")})

    # -- BigQuery REST v2 --
    def _bigquery(self, method: str, path: str, query: dict) -> None:
        principal = self._principal()
        body = json.loads(self._body() or b"{}") if method == "POST" else {}
        if principal is None:
            _audit("bigquery", ok=False, method=method, path=path, principal=None)
            return self._error(401, "Request had invalid authentication credentials.", "authError")
        if principal not in _state["bigquery_principals"]:
            _audit("bigquery", ok=False, method=method, path=path, principal=principal)
            return self._error(403, f"Access Denied: Project {PROJECT}: {principal} does not have bigquery.jobs.create permission.",
                               "accessDenied")
        path = path.removeprefix("/bigquery/v2")

        if method == "GET" and re.fullmatch(r"/projects/[^/]+/datasets", path):
            _audit("bigquery", ok=True, call="datasets.list", principal=principal)
            return self._send(200, {"kind": "bigquery#datasetList", "datasets": [{
                "kind": "bigquery#dataset", "id": f"{PROJECT}:sales",
                "datasetReference": {"projectId": PROJECT, "datasetId": "sales"}, "location": "US"}]})

        m = re.fullmatch(r"/projects/[^/]+/datasets/([^/]+)/tables/([^/]+)(/data)?", path)
        if method == "GET" and m:
            table = m.group(2)
            db = _state["db"].cursor()
            if m.group(1) != "sales" or table not in _tables(db):
                return self._error(404, f"Not found: Table {PROJECT}:{m.group(1)}.{table}", "notFound")
            cols = _columns(db, table)
            if m.group(3):
                limit = int((query.get("maxResults") or ["100"])[0])
                rows = db.execute(f'SELECT * FROM "{table}" LIMIT {limit}').fetchall()
                total = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                _audit("bigquery", ok=True, call="tabledata.list", table=table, principal=principal)
                return self._send(200, {"kind": "bigquery#tableDataList", "totalRows": str(total), "rows": [
                    {"f": [{"v": _cell(v, cols[i][1])} for i, v in enumerate(r)]} for r in rows]})
            total = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            _audit("bigquery", ok=True, call="tables.get", table=table, principal=principal)
            return self._send(200, {
                "kind": "bigquery#table", "id": f"{PROJECT}:sales.{table}", "type": "TABLE", "location": "US",
                "tableReference": {"projectId": PROJECT, "datasetId": "sales", "tableId": table},
                "schema": {"fields": [{"name": n, "type": t, "mode": "NULLABLE"} for n, t in cols]},
                "numRows": str(total), "numBytes": str(total * 64)})

        if method == "POST" and re.fullmatch(r"/projects/[^/]+/jobs", path):
            cfg = (body.get("configuration") or {})
            sql = ((cfg.get("query") or {}).get("query")) or ""
            job_id = ((body.get("jobReference") or {}).get("jobId")) or uuid.uuid4().hex
            ref = {"projectId": PROJECT, "jobId": job_id, "location": "US"}
            job = {"kind": "bigquery#job", "id": f"{PROJECT}:US.{job_id}", "jobReference": ref, "user_email": principal,
                   "configuration": {"jobType": "QUERY", "query": cfg.get("query") or {}, "dryRun": bool(cfg.get("dryRun"))},
                   "status": {"state": "DONE"},
                   "statistics": {"creationTime": "0", "startTime": "0", "endTime": "0",
                                  "totalBytesProcessed": "512", "query": {"statementType": "SELECT",
                                                                         "totalBytesProcessed": "512",
                                                                         "totalBytesBilled": "0", "cacheHit": False}}}
            try:
                schema, rows = run_sql(sql)
            except Exception as e:
                _audit("bigquery", ok=False, call="jobs.insert", principal=principal, sql=sql, problem=str(e))
                return self._error(400, f"Syntax error or unsupported query: {e}", "invalidQuery")
            job["statistics"]["query"]["schema"] = {"fields": schema}
            with _lock:
                _jobs[job_id] = {"job": job, "schema": schema, "rows": rows}
            _audit("bigquery", ok=True, call="jobs.insert", principal=principal, sql=" ".join(sql.split()),
                   rows=len(rows), dry_run=bool(cfg.get("dryRun")))
            return self._send(200, job)

        m = re.fullmatch(r"/projects/[^/]+/jobs/([^/]+)(/cancel)?", path)
        if m and m.group(1) in _jobs:
            job = _jobs[m.group(1)]["job"]
            return self._send(200, {"kind": "bigquery#jobCancelResponse", "job": job} if m.group(2) else job)

        m = re.fullmatch(r"/projects/[^/]+/queries/([^/]+)", path)
        if method == "GET" and m and m.group(1) in _jobs:
            done = _jobs[m.group(1)]
            return self._send(200, {
                "kind": "bigquery#getQueryResultsResponse", "jobComplete": True,
                "jobReference": done["job"]["jobReference"], "schema": {"fields": done["schema"]},
                "totalRows": str(len(done["rows"])), "totalBytesProcessed": "512", "cacheHit": False,
                "rows": [{"f": [{"v": v} for v in row]} for row in done["rows"]]})

        _audit("bigquery", ok=False, method=method, path=path, principal=principal, problem="unhandled")
        self._error(404, f"fake_google_cloud does not implement {method} {path}", "notFound")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", default="/tmp/fake-google")
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--impersonable", action="append",
                    help=f"service account the federated principal may impersonate (repeatable; default {DEFAULT_IMPERSONABLE})")
    ap.add_argument("--bigquery-principals", choices=["both", "federated", "service-account"], default="both",
                    help="who has BigQuery access: the federated principal, the impersonable service accounts, or both")
    ap.add_argument("--write-hosts", action="store_true", help="point the three hostnames at 127.0.0.1 in /etc/hosts")
    ap.add_argument("--remove-hosts", action="store_true", help="remove that /etc/hosts line and exit")
    args = ap.parse_args()

    if args.remove_hosts:
        edit_hosts(add=False)
        print("removed the fake_google_cloud line from /etc/hosts")
        return

    run_dir = Path(args.run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    ca_path, cert_path, key_path = write_tls(run_dir)
    bundle = write_trust_bundle(run_dir, ca_path)
    federation = write_federation_config(run_dir, f"fake-oidc-token.{uuid.uuid4().hex}")
    legacy_key = write_legacy_key(run_dir)
    if args.write_hosts:
        edit_hosts(add=True)

    impersonable = set(args.impersonable or [DEFAULT_IMPERSONABLE])
    principals = set()
    if args.bigquery_principals in ("both", "federated"):
        principals.add(FEDERATED_PRINCIPAL)
    if args.bigquery_principals in ("both", "service-account"):
        principals |= {f"serviceAccount:{sa}" for sa in impersonable}
    principals.add(f"serviceAccount:{LEGACY_KEY_ACCOUNT}")
    audit = run_dir / "bigquery_audit.jsonl"
    audit.write_text("")
    _state.update(db=build_warehouse(), audit=str(audit), impersonable=impersonable,
                  bigquery_principals=principals, subject_token_path=str(run_dir / "subject-token.jwt"))

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)

    no_proxy = ",".join(filter(None, [os.environ.get("NO_PROXY", ""), *HOSTS]))
    print(f"""fake Google Cloud is listening on 127.0.0.1:{args.port} as {', '.join(HOSTS)}

project            {PROJECT}   (dataset: sales; tables: customers, orders)
federated identity {FEDERATED_PRINCIPAL}
may impersonate    {', '.join(sorted(impersonable))}
BigQuery access    {', '.join(sorted(principals))}
legacy key file    {legacy_key}   (a pasted-key connection, for testing a switch)
audit log          {audit}

Start the backend with:

  export BOW_ALLOW_GOOGLE_DEFAULT_CREDENTIALS=true
  export GOOGLE_APPLICATION_CREDENTIALS={federation}
  export REQUESTS_CA_BUNDLE={bundle}
  export SSL_CERT_FILE={bundle}
  export NO_PROXY={no_proxy}
  export no_proxy={no_proxy}
""", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())

"""Reproduce "certificate verify failed: Missing Authority Key Identifier".

Stands up a local HTTPS server whose certificate chain is shaped like the one a
TLS-inspecting proxy with a hand-made CA presents: the leaf is CA-issued but
carries no Authority Key Identifier extension. Python 3.13 verifies with
OpenSSL's strict X.509 rules by default and rejects it; the same chain passes
with `security.tls_strict_x509=false`.

Runs both policies in one process and exits 0 only when strict FAILS with the
reported error and relaxed PASSES. No network, no secrets.

    cd backend && uv run python ../tools/agent/tls_strict_x509_probe.py
"""
from __future__ import annotations

import datetime as dt
import ssl
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.core.tls import configure_process_tls  # noqa: E402  (run from backend/)


def _chain_without_aki(tmp: Path) -> tuple[Path, Path, Path]:
    now = dt.datetime.now(dt.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Corp TLS Inspection CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name).public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        # Deliberately no AuthorityKeyIdentifier: the defect under test.
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem = tmp / "ca.pem"
    ca_pem.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    leaf_pem = tmp / "leaf.pem"
    leaf_pem.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_pem = tmp / "leaf.key"
    key_pem.write_bytes(leaf_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return ca_pem, leaf_pem, key_pem


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b'{"issuer": "https://localhost/", "token_endpoint": "https://localhost/token"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def _serve(leaf_pem: Path, key_pem: Path) -> tuple[HTTPServer, int]:
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(str(leaf_pem), str(key_pem))
    srv.socket = server_ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _probe(url: str, ca_pem: Path) -> str:
    """One line per client family: httpx (httpx-oauth, LLM SDKs) and requests
    (urllib3: MSAL / azure-identity and most connectors). They build their SSL
    contexts differently, so both must flip."""
    out = []
    try:
        r = httpx.get(url, verify=str(ca_pem), timeout=5)
        out.append(f"httpx PASS HTTP {r.status_code}")
    except httpx.ConnectError as e:
        out.append(f"httpx FAIL {e}")
    try:
        r = requests.get(url, verify=str(ca_pem), timeout=5)
        out.append(f"requests PASS HTTP {r.status_code}")
    except requests.exceptions.SSLError as e:
        reason = str(e).split("(Caused by")[-1].split("SSLError(")[-1].rstrip(")")
        out.append(f"requests FAIL {reason}")
    return " | ".join(out)


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        ca_pem, leaf_pem, key_pem = _chain_without_aki(Path(d))
        srv, port = _serve(leaf_pem, key_pem)
        url = f"https://localhost:{port}/.well-known/openid-configuration"
        try:
            configure_process_tls(strict_x509=True)
            strict = _probe(url, ca_pem)
            configure_process_tls(strict_x509=False)
            relaxed = _probe(url, ca_pem)
        finally:
            srv.shutdown()
            configure_process_tls(strict_x509=True)
    print(f"python            {sys.version.split()[0]}")
    print(f"strict_x509=true  {strict}")
    print(f"strict_x509=false {relaxed}")
    ok = strict.count("Missing Authority Key Identifier") == 2 and relaxed.count("PASS") == 2
    print("RESULT", "OK" if ok else "UNEXPECTED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

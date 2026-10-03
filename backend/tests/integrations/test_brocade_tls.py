"""Real HTTPS trust check with an ephemeral certificate and synthetic API."""
import importlib.util
import ipaddress
import ssl
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.data_sources.clients.brocade_client import BrocadeClient, BrocadeError


def test_deployment_ca_enforces_chain_and_hostname(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        'brocade_tls_sim', Path(__file__).resolve().parents[3] / 'tools/brocade/simulated_api.py')
    sim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Synthetic Brocade TLS test')])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
        .sign(key, hashes.SHA256()))
    ca_path, key_path = tmp_path / 'ca.pem', tmp_path / 'key.pem'
    ca_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_path.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ca_path, key_path)
    server = ThreadingHTTPServer(('127.0.0.1', 0), sim.Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    monkeypatch.setenv('BROCADE_SIM_USER', 'lab-reader')
    monkeypatch.setenv('BROCADE_SIM_PASSWORD', 'synthetic-only')
    thread.start()
    port = server.server_address[1]
    try:
        monkeypatch.delenv('REQUESTS_CA_BUNDLE', raising=False)
        client = BrocadeClient(f'https://127.0.0.1:{port}', 'lab-reader', 'synthetic-only')
        with pytest.raises(BrocadeError, match='TransportError'):
            client.get_tables()
        monkeypatch.setenv('REQUESTS_CA_BUNDLE', str(ca_path))
        assert len(client.get_tables()) == 12
        wrong_hostname = BrocadeClient(f'https://localhost:{port}', 'lab-reader', 'synthetic-only')
        with pytest.raises(BrocadeError, match='TransportError'):
            wrong_hostname.get_tables()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

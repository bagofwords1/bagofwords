"""Registry-created Cisco APIC connections authenticate and index the catalog through the real API.

Both auth variants run against the in-repo APIC simulator over loopback HTTP, so
the stored credentials, the schema-driven form fields and the client resolution
are exercised exactly as the connect form uses them.
"""

import importlib.util
import sys
import threading
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_SIM = Path(__file__).parents[3] / "tools/cisco_apic/simulated_api.py"


@pytest.fixture
def apic(tmp_path, monkeypatch):
    from http.server import ThreadingHTTPServer

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = tmp_path / "user-cert.pem"
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    for k, v in {"APIC_USERNAME": "bow-ro", "APIC_PASSWORD": "sim-only-password", "APIC_CERT_USERNAME": "bow-cert",
                 "APIC_CERT_NAME": "bow-signing", "APIC_CERT_PEM": str(public)}.items():
        monkeypatch.setenv(k, v)
    if "cisco_apic_simulated_api" not in sys.modules:
        spec = importlib.util.spec_from_file_location("cisco_apic_simulated_api", _SIM)
        module = importlib.util.module_from_spec(spec)
        sys.modules["cisco_apic_simulated_api"] = module
        spec.loader.exec_module(module)
    mock = sys.modules["cisco_apic_simulated_api"]
    sim = mock.Simulator()
    server = ThreadingHTTPServer(("127.0.0.1", 0), mock.make_handler(sim))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    yield f"http://127.0.0.1:{server.server_address[1]}", sim, pem
    server.shutdown()


@pytest.mark.e2e
def test_cisco_apic_connections_authenticate_and_index_catalog(
    create_user,
    login_user,
    whoami,
    create_data_source,
    refresh_schema,
    test_connection,
    delete_data_source,
    apic,
):
    url, sim, pem = apic
    user = create_user()
    token = login_user(user["email"], user["password"])
    org = whoami(token)["organizations"][0]["id"]
    base = {"url": url, "fallback_urls": "", "login_domain": "", "ca_certificate": "", "allow_http": True}

    cert = create_data_source(
        name="ACI fabric (certificate)",
        type="cisco_apic",
        config=base,
        credentials={"username": "bow-cert", "certificate_name": "bow-signing", "private_key": pem},
        user_token=token,
        org_id=org,
    )
    result = test_connection(data_source_id=cert["id"], user_token=token, org_id=org)
    assert result["success"] and "6.2(1g)" in result["message"], result
    logins_after_cert = sim.logins
    assert logins_after_cert == 0  # signed requests never open a session

    password = create_data_source(
        name="ACI fabric (password)",
        type="cisco_apic",
        config={**base, "auth_type": "userpass"},
        credentials={"username": "bow-ro", "password": "sim-only-password"},
        user_token=token,
        org_id=org,
    )
    result = test_connection(data_source_id=password["id"], user_token=token, org_id=org)
    assert result["success"], result
    assert sim.logins > 0 and sim.sessions == {}  # every session was logged out

    calls = len(sim.log)
    tables = refresh_schema(data_source_id=cert["id"], user_token=token, org_id=org)
    names = {t["name"] for t in tables}
    assert {"fabric_nodes", "faults", "events", "audit_log", "interface_status", "bgp_peers", "endpoints", "contracts"} <= names
    faults = next(t for t in tables if t["name"] == "faults")
    assert faults["metadata_json"]["cisco_apic"]["class"] == "faultInst"
    assert {"severity", "parent_dn", "node"} <= {c["name"] for c in faults["columns"]}
    assert len(sim.log) == calls  # schema discovery is catalog driven: no controller calls
    assert sim.writes == [] and sim.subscriptions == []

    delete_data_source(data_source_id=cert["id"], user_token=token, org_id=org)
    delete_data_source(data_source_id=password["id"], user_token=token, org_id=org)

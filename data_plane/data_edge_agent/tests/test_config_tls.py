"""TLS/mTLS config surface (design A11): defaults, env overrides, and the
SSLContext built from the configured cert paths."""

from __future__ import annotations

import ssl
import subprocess

import pytest

from ..config import AgentConfig, load_config


def _selfsigned(dir_path, name: str) -> tuple[str, str]:
    """Generate a throwaway self-signed cert+key with openssl.

    We use openssl rather than the `cryptography` package: it was dropped from
    the data_plane dependencies, and load_cert_chain only needs valid PEM files.
    """
    cert = dir_path / f"{name}.pem"
    key = dir_path / f"{name}-key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert),
            "-days", "1", "-subj", f"/CN={name}",
        ],
        check=True,
        capture_output=True,
    )
    return str(cert), str(key)


def test_tls_defaults():
    cfg = AgentConfig(org_id="cust-b", edge_agent_id="nyc-01")
    assert cfg.tls_ca is None
    assert cfg.tls_cert is None
    assert cfg.tls_key is None
    assert cfg.tls_verify is True


def test_tls_context_requires_client_cert():
    # mTLS is the only auth path: no client cert -> hard error, not plaintext.
    cfg = AgentConfig(org_id="cust-b", edge_agent_id="nyc-01")
    with pytest.raises(ValueError, match="mTLS-only"):
        cfg.tls_context()


def test_tls_env_overrides(monkeypatch):
    monkeypatch.setenv("BOW_EDGE_AGENT_ORG_ID", "cust-b")
    monkeypatch.setenv("BOW_EDGE_AGENT_EDGE_AGENT_ID", "nyc-01")
    monkeypatch.setenv("BOW_EDGE_AGENT_TLS_CA", "/etc/bow/ca.pem")
    monkeypatch.setenv("BOW_EDGE_AGENT_TLS_CERT", "/etc/bow/agent.pem")
    monkeypatch.setenv("BOW_EDGE_AGENT_TLS_KEY", "/etc/bow/agent-key.pem")
    cfg = load_config(None)
    assert cfg.tls_ca == "/etc/bow/ca.pem"
    assert cfg.tls_cert == "/etc/bow/agent.pem"
    assert cfg.tls_key == "/etc/bow/agent-key.pem"


@pytest.mark.parametrize("raw,expected", [
    ("0", False), ("false", False), ("no", False), ("off", False),
    ("1", True), ("true", True), ("yes", True), ("ON", True),
])
def test_tls_verify_env_coercion(monkeypatch, raw, expected):
    monkeypatch.setenv("BOW_EDGE_AGENT_ORG_ID", "cust-b")
    monkeypatch.setenv("BOW_EDGE_AGENT_EDGE_AGENT_ID", "nyc-01")
    monkeypatch.setenv("BOW_EDGE_AGENT_TLS_VERIFY", raw)
    assert load_config(None).tls_verify is expected


def test_tls_context_ca_only_still_requires_client_cert(tmp_path):
    # A CA alone verifies the broker but does not authenticate us; without a
    # client cert there is no auth at all, so it must raise.
    ca, _ = _selfsigned(tmp_path, "ca")
    cfg = AgentConfig(org_id="cust-b", edge_agent_id="nyc-01", tls_ca=ca)
    with pytest.raises(ValueError, match="mTLS-only"):
        cfg.tls_context()


def test_tls_context_verify_mode_defaults(tmp_path):
    # With a client cert present the context verifies the broker by default.
    ca, _ = _selfsigned(tmp_path, "ca")
    cert, key = _selfsigned(tmp_path, "edge-agent")
    cfg = AgentConfig(
        org_id="cust-b", edge_agent_id="nyc-01",
        tls_ca=ca, tls_cert=cert, tls_key=key,
    )
    ctx = cfg.tls_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_tls_context_mtls(tmp_path):
    ca, _ = _selfsigned(tmp_path, "ca")
    cert, key = _selfsigned(tmp_path, "edge-agent")
    cfg = AgentConfig(
        org_id="cust-b", edge_agent_id="nyc-01",
        tls_ca=ca, tls_cert=cert, tls_key=key,
    )
    # load_cert_chain raising would fail the call; reaching here means the
    # client cert loaded.
    assert isinstance(cfg.tls_context(), ssl.SSLContext)


def test_tls_context_verify_off(tmp_path):
    cert, key = _selfsigned(tmp_path, "edge-agent")
    cfg = AgentConfig(
        org_id="cust-b", edge_agent_id="nyc-01",
        tls_cert=cert, tls_key=key, tls_verify=False,
    )
    ctx = cfg.tls_context()
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_tls_context_half_mtls_raises(tmp_path):
    # A cert without its key is a config error, not a half-armed mTLS.
    cert, _ = _selfsigned(tmp_path, "edge-agent")
    cfg = AgentConfig(org_id="cust-b", edge_agent_id="nyc-01", tls_cert=cert)
    with pytest.raises(ValueError, match="both tls_cert and tls_key"):
        cfg.tls_context()

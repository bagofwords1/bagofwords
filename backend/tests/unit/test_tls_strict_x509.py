"""Operators can opt out of Python 3.13's strict X.509 checks.

Python 3.13 builds every default ``SSLContext`` with ``VERIFY_X509_STRICT``,
which rejects certificates lacking an Authority Key Identifier. Behind a
TLS-inspecting proxy with a hand-made CA that fails every outbound HTTPS call
("certificate verify failed: Missing Authority Key Identifier"), and the
libraries involved (httpx-oauth, the LLM SDKs) give no way to hand in a
context. ``security.tls_strict_x509=false`` must relax that one flag for every
context built afterwards, and nothing else: certificates must still be
verified and hostnames still checked.
"""
import ssl

import httpx
import pytest

from app.core import tls
from app.settings.bow_config import SecurityConfig


@pytest.fixture
def process_tls():
    """Restore the interpreter's own factory and the cached context afterwards."""
    yield
    tls.configure_process_tls(strict_x509=True)
    tls.shared_ssl_context.cache_clear()


def _strict(ctx: ssl.SSLContext) -> bool:
    return bool(ctx.verify_flags & ssl.VERIFY_X509_STRICT)


def test_default_policy_keeps_the_interpreter_default(process_tls):
    tls.configure_process_tls(strict_x509=True)
    tls.shared_ssl_context.cache_clear()
    assert _strict(ssl.create_default_context()) == _strict(tls._create_default_context())
    assert _strict(tls.shared_ssl_context()) == _strict(tls._create_default_context())


def test_relaxed_policy_clears_strict_flag_for_every_client_path(process_tls):
    tls.configure_process_tls(strict_x509=False)
    tls.shared_ssl_context.cache_clear()
    # Direct callers, httpx's own factory (what httpx-oauth and the SDKs use),
    # and the shared context all go through the same policy.
    for ctx in (ssl.create_default_context(), httpx.create_ssl_context(), tls.shared_ssl_context()):
        assert not _strict(ctx)


def test_relaxed_policy_still_verifies_certificates_and_hostnames(process_tls):
    tls.configure_process_tls(strict_x509=False)
    ctx = ssl.create_default_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_policy_is_idempotent_and_reversible(process_tls):
    for _ in range(2):
        tls.configure_process_tls(strict_x509=False)
    assert not _strict(ssl.create_default_context())
    tls.configure_process_tls(strict_x509=True)
    assert _strict(ssl.create_default_context()) == _strict(tls._create_default_context())


@pytest.mark.parametrize(
    "value,expected",
    [(None, True), ("true", True), ("1", True), ("false", False), ("0", False), ("no", False), (" OFF ", False)],
)
def test_config_reads_env_override(monkeypatch, value, expected):
    monkeypatch.delenv("BOW_TLS_STRICT_X509", raising=False)
    if value is not None:
        monkeypatch.setenv("BOW_TLS_STRICT_X509", value)
    assert SecurityConfig().tls_strict_x509 is expected


def test_config_yaml_value_wins_over_default(monkeypatch):
    monkeypatch.delenv("BOW_TLS_STRICT_X509", raising=False)
    assert SecurityConfig(tls_strict_x509=False).tls_strict_x509 is False

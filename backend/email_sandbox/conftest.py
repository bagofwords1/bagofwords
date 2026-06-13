"""Sandbox harness for the email integration.

This directory deliberately lives *outside* ``backend/tests`` so it can run in
environments where the full app dependency stack (psycopg2/thrift/etc.) isn't
installed. It exercises the real email mechanics against a live local SMTP
server with only ``pytest``, ``aiosmtplib`` and ``aiosmtpd``.

The email *adapter* imports ``app.settings.config`` and (via ``base_adapter``)
``app.models`` — both heavy. We stub just those so the real
``app.services.email.*`` transport code and the real ``EmailAdapter`` load
unchanged (same technique as ``tests/unit/test_whatsapp_adapter.py``).
"""
from __future__ import annotations

import socket
import sys
import types
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _install_stubs() -> None:
    """Stub the heavy modules the adapter pulls in, leaving the rest real."""
    if "app.settings.config" not in sys.modules:
        settings_pkg = types.ModuleType("app.settings")
        config_mod = types.ModuleType("app.settings.config")
        bow_cfg = types.SimpleNamespace(base_url="http://localhost:3000")
        config_mod.settings = types.SimpleNamespace(bow_config=bow_cfg, email_client=None)
        sys.modules["app.settings"] = settings_pkg
        sys.modules["app.settings.config"] = config_mod

    if "app.models.external_platform" not in sys.modules:
        models_pkg = types.ModuleType("app.models")
        ep_mod = types.ModuleType("app.models.external_platform")

        class _EP:  # noqa: N801
            pass

        ep_mod.ExternalPlatform = _EP
        eum_mod = types.ModuleType("app.models.external_user_mapping")

        class _EUM:  # noqa: N801
            pass

        eum_mod.ExternalUserMapping = _EUM
        sys.modules["app.models"] = models_pkg
        sys.modules["app.models.external_platform"] = ep_mod
        sys.modules["app.models.external_user_mapping"] = eum_mod


_install_stubs()


# ---------------------------------------------------------------------------
# Fakes shared across tests
# ---------------------------------------------------------------------------


class FakePlatform:
    """Stands in for an ExternalPlatform row (config + decrypted creds)."""

    def __init__(self, credentials: dict, config: dict | None = None):
        self._credentials = credentials or {}
        self.platform_config = config or {}

    def decrypt_credentials(self) -> dict:
        return dict(self._credentials)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _SinkHandler:
    """Captures every message the SMTP sink receives (raw bytes)."""

    def __init__(self) -> None:
        self.messages: list[bytes] = []
        self.rcpts: list[list[str]] = []

    async def handle_DATA(self, server, session, envelope):  # noqa: N802 (aiosmtpd API)
        self.messages.append(envelope.content)
        self.rcpts.append(list(envelope.rcpt_tos))
        return "250 Message accepted"


@pytest.fixture
def smtp_sink():
    """Run a real local SMTP server; yield (host, port, handler)."""
    from aiosmtpd.controller import Controller

    handler = _SinkHandler()
    port = free_port()
    controller = Controller(handler, hostname="127.0.0.1", port=port)
    controller.start()
    try:
        yield "127.0.0.1", port, handler
    finally:
        controller.stop()


@pytest.fixture
def make_email_adapter():
    """Factory returning an EmailAdapter wired to given creds/config."""
    from app.services.platform_adapters.email_adapter import EmailAdapter

    def _make(credentials: dict, config: dict | None = None) -> EmailAdapter:
        return EmailAdapter(FakePlatform(credentials, config))

    return _make

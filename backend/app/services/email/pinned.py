"""SMTP / IMAP clients that connect only to a validated address (SSRF guard).

The SMTP and IMAP hosts are admin-entered. ``aiosmtplib`` and ``imaplib``
would each resolve the name on their own; these subclasses resolve it once
through :mod:`app.core.egress` (admin tier: cloud metadata / link-local always
refused, private / LAN only with ``security.block_private_outbound``), connect
the socket to the validated address, and keep the hostname for implicit TLS,
STARTTLS, SNI and certificate verification.
"""
from __future__ import annotations

import imaplib
from typing import Any

import aiosmtplib

from app.core.egress import Policy, connect_pinned_socket, open_pinned_socket


class PinnedSMTP(aiosmtplib.SMTP):
    """``aiosmtplib.SMTP`` whose connect goes to a validated address.

    ``aiosmtplib`` accepts a pre-connected ``sock`` and then uses
    ``hostname`` for the TLS server name (implicit TLS and STARTTLS alike),
    so the certificate is still verified against the configured host.
    """

    egress_policy = Policy.ADMIN

    async def connect(self, *args: Any, **kwargs: Any):
        for key in ("hostname", "port"):
            if key in kwargs:
                setattr(self, key, kwargs.pop(key))
        if self.sock is not None or self.socket_path is not None:
            return await super().connect(*args, **kwargs)
        host = self.hostname or "localhost"
        port = self.port or self._get_default_port()
        timeout = kwargs.get("timeout", self.timeout)
        if not isinstance(timeout, (int, float)):
            timeout = self.timeout
        self.sock = await open_pinned_socket(host, port, self.egress_policy, timeout=timeout)
        self.hostname, self.port = host, None
        try:
            return await super().connect(*args, **kwargs)
        finally:
            # The socket now belongs to the transport; a later reconnect on
            # this instance must resolve and validate again.
            self.sock, self.port = None, port


async def send_pinned(msg, **kwargs: Any):
    """``aiosmtplib.send`` over :class:`PinnedSMTP` (same keyword args)."""
    async with PinnedSMTP(**kwargs) as client:
        return await client.send_message(msg)


class _PinnedIMAPMixin:
    egress_policy = Policy.ADMIN

    def _create_socket(self, timeout):  # imaplib's own hook (3.9+)
        if timeout is not None and not timeout:
            raise ValueError("Non-blocking socket (timeout=0) is not supported")
        return connect_pinned_socket(self.host, self.port, self.egress_policy, timeout=timeout)


class PinnedIMAP4(_PinnedIMAPMixin, imaplib.IMAP4):
    pass


class PinnedIMAP4_SSL(imaplib.IMAP4_SSL, PinnedIMAP4):
    """``IMAP4_SSL._create_socket`` wraps ``super()._create_socket()`` —
    which resolves to the pinned connect — with ``server_hostname=self.host``."""

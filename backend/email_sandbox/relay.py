"""A local SMTP relay that can say **no**.

The existing ``smtp_sink`` fixture accepts everything, which makes it useless
for the failure mode that actually bites: a configuration that connects and
authenticates fine and still never delivers a message. A relay that cannot
reject cannot tell a fixed test from a broken one.

This relay models the four refusals real servers hand out:

===================  ===========================================
``require_auth``     ``535`` unless the exact username/password
                     is offered — catches a password that failed
                     to decrypt, or a username saved with no
                     password, both of which a connect-and-auth
                     probe silently skips.
``allowed_senders``  ``550`` on ``MAIL FROM`` for an envelope
                     sender the relay will not vouch for.
``allowed_rcpts``    ``550`` on ``RCPT TO`` — "relay denied".
``require_tls``      ``530 5.7.0 Must issue a STARTTLS command
                     first`` while the session is in plaintext.
===================  ===========================================

Usage::

    with run_relay(require_auth=("user", "pw")) as relay:
        ...                      # relay.host, relay.port
        assert relay.messages     # raw bytes of everything accepted
"""
from __future__ import annotations

import socket
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from email import message_from_bytes
from email.message import Message


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Authenticator:
    """aiosmtpd authenticator accepting exactly one username/password pair."""

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password

    def __call__(self, server, session, envelope, mechanism, auth_data):
        from aiosmtpd.smtp import AuthResult, LoginPassword

        fail = AuthResult(success=False, handled=False)
        if not isinstance(auth_data, LoginPassword):
            return fail
        username = auth_data.login.decode("utf-8", "replace")
        password = auth_data.password.decode("utf-8", "replace")
        if username == self._username and password == self._password:
            return AuthResult(success=True)
        return fail


@dataclass
class RelayHandler:
    """Records accepted mail and enforces the configured refusals."""

    allowed_senders: Sequence[str] | None = None
    allowed_rcpts: Sequence[str] | None = None
    require_tls: bool = False

    messages: list[bytes] = field(default_factory=list)
    senders: list[str] = field(default_factory=list)
    rcpts: list[list[str]] = field(default_factory=list)

    # -- aiosmtpd hooks -------------------------------------------------
    async def handle_MAIL(self, server, session, envelope, address, mail_options):  # noqa: N802
        if self.require_tls and not getattr(session, "ssl", None):
            return "530 5.7.0 Must issue a STARTTLS command first"
        if self.allowed_senders is not None and address not in self.allowed_senders:
            return f"550 5.7.1 Sender address {address} not owned by user"
        envelope.mail_from = address
        envelope.mail_options.extend(mail_options)
        return "250 OK"

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):  # noqa: N802
        if self.allowed_rcpts is not None and address not in self.allowed_rcpts:
            return f"550 5.7.1 Relay access denied for {address}"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):  # noqa: N802
        self.messages.append(envelope.content)
        self.senders.append(envelope.mail_from)
        self.rcpts.append(list(envelope.rcpt_tos))
        return "250 Message accepted"

    # -- assertions helpers ---------------------------------------------
    @property
    def count(self) -> int:
        return len(self.messages)

    def parsed(self, index: int = -1) -> Message:
        return message_from_bytes(self.messages[index])

    def subjects(self) -> list[str]:
        return [message_from_bytes(m).get("Subject", "") for m in self.messages]

    def body(self, index: int = -1) -> str:
        msg = self.parsed(index)
        if msg.is_multipart():
            parts = [
                p.get_payload(decode=True) or b""
                for p in msg.walk()
                if p.get_content_maintype() == "text"
            ]
            return b"\n".join(parts).decode("utf-8", "replace")
        return (msg.get_payload(decode=True) or b"").decode("utf-8", "replace")

    def find(self, needle: str) -> list[Message]:
        """Every accepted message whose subject or body contains ``needle``."""
        out = []
        for i in range(len(self.messages)):
            if needle in self.parsed(i).get("Subject", "") or needle in self.body(i):
                out.append(self.parsed(i))
        return out

    def clear(self) -> None:
        self.messages.clear()
        self.senders.clear()
        self.rcpts.clear()


@dataclass
class Relay:
    host: str
    port: int
    handler: RelayHandler

    @property
    def messages(self) -> list[bytes]:
        return self.handler.messages

    @property
    def count(self) -> int:
        return self.handler.count


@contextmanager
def run_relay(
    *,
    require_auth: tuple[str, str] | None = None,
    allowed_senders: Sequence[str] | None = None,
    allowed_rcpts: Sequence[str] | None = None,
    require_tls: bool = False,
    port: int | None = None,
) -> Iterator[Relay]:
    """Run a rejecting SMTP relay on localhost for the duration of the block."""
    from aiosmtpd.controller import Controller

    handler = RelayHandler(
        allowed_senders=allowed_senders,
        allowed_rcpts=allowed_rcpts,
        require_tls=require_tls,
    )
    kwargs = {}
    if require_auth is not None:
        kwargs.update(
            authenticator=_Authenticator(*require_auth),
            auth_required=True,
            # The sandbox speaks plaintext; without this aiosmtpd refuses AUTH
            # outside TLS, which would mask the auth assertions under a 538.
            auth_require_tls=False,
        )
    chosen = port or free_port()
    controller = Controller(handler, hostname="127.0.0.1", port=chosen, **kwargs)
    controller.start()
    try:
        yield Relay(host="127.0.0.1", port=chosen, handler=handler)
    finally:
        controller.stop()

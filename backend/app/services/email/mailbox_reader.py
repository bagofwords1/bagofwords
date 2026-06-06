"""Inbound mailbox transport.

Email has no native webhook, so to *receive* mail we connect to the analyst
mailbox and pull new messages. This is abstracted behind ``MailboxReader`` so:

  * the poller depends on an interface, not on IMAP details, and
  * tests (and the sandbox feedback loop) can drive the exact same poller with
    a ``FakeMailboxReader`` fed canned RFC822 bytes — no live IMAP server needed.

``ImapMailboxReader`` is the production implementation over stdlib ``imaplib``
(run in a thread so it doesn't block the event loop). v1 polls UNSEEN; IMAP
IDLE is a future enhancement noted in the design doc.
"""
from __future__ import annotations

import asyncio
import imaplib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ImapConfig:
    host: str
    port: int = 993
    username: Optional[str] = None
    password: Optional[str] = None
    use_ssl: bool = True
    mailbox: str = "INBOX"
    auth_type: str = "password"
    oauth: object = None  # OAuthSettings when auth_type is microsoft/google

    @classmethod
    def from_credentials(cls, creds: dict, config: Optional[dict] = None) -> "ImapConfig":
        from app.services.email.oauth import OAuthSettings

        creds = creds or {}
        config = config or {}
        return cls(
            host=creds.get("imap_host") or config.get("imap_host"),
            port=int(creds.get("imap_port") or config.get("imap_port") or 993),
            username=creds.get("imap_username") or creds.get("username"),
            password=creds.get("imap_password") or creds.get("password"),
            use_ssl=bool(config.get("imap_use_ssl", True)),
            mailbox=config.get("imap_mailbox", "INBOX"),
            auth_type=(creds.get("auth_type") or config.get("auth_type") or "password"),
            oauth=OAuthSettings.from_credentials(creds, config),
        )


class MailboxReader(ABC):
    """Reads new messages from a mailbox."""

    @abstractmethod
    async def fetch_unseen(self) -> List[Tuple[str, bytes]]:
        """Return a list of ``(uid, raw_rfc822_bytes)`` for unread messages."""

    @abstractmethod
    async def mark_seen(self, uid: str) -> None:
        """Mark a message as processed so it isn't returned again."""

    async def close(self) -> None:  # pragma: no cover - optional
        return None


class FakeMailboxReader(MailboxReader):
    """In-memory reader for tests and the sandbox loopback.

    Push raw messages with :meth:`deliver`; ``fetch_unseen`` returns the unread
    ones and ``mark_seen`` removes them from the unread set.
    """

    def __init__(self) -> None:
        self._messages: dict[str, bytes] = {}
        self._unseen: set[str] = set()
        self._counter = 0

    def deliver(self, raw: bytes) -> str:
        self._counter += 1
        uid = str(self._counter)
        self._messages[uid] = raw
        self._unseen.add(uid)
        return uid

    async def fetch_unseen(self) -> List[Tuple[str, bytes]]:
        return [(uid, self._messages[uid]) for uid in sorted(self._unseen, key=int)]

    async def mark_seen(self, uid: str) -> None:
        self._unseen.discard(uid)


class ImapMailboxReader(MailboxReader):
    """Production IMAP reader over stdlib ``imaplib`` (blocking calls offloaded)."""

    def __init__(self, config: ImapConfig) -> None:
        self.config = config

    def _connect(self, xoauth2: Optional[str] = None) -> imaplib.IMAP4:
        cfg = self.config
        if cfg.use_ssl:
            conn = imaplib.IMAP4_SSL(cfg.host, cfg.port)
        else:
            conn = imaplib.IMAP4(cfg.host, cfg.port)
        if cfg.auth_type in ("microsoft", "google"):
            # SASL XOAUTH2: the token string is minted in the async layer and
            # passed in; imaplib hands the server our initial response.
            if not xoauth2:
                raise ValueError("xoauth2 token required for OAuth IMAP auth")
            conn.authenticate("XOAUTH2", lambda _challenge: xoauth2.encode("ascii"))
        else:
            conn.login(cfg.username, cfg.password)
        return conn

    async def _xoauth2_string(self) -> Optional[str]:
        if self.config.auth_type in ("microsoft", "google") and self.config.oauth:
            from app.services.email.oauth import get_xoauth2_string

            return await get_xoauth2_string(self.config.oauth)
        return None

    def _fetch_unseen_blocking(self, xoauth2: Optional[str] = None) -> List[Tuple[str, bytes]]:
        conn = self._connect(xoauth2)
        out: List[Tuple[str, bytes]] = []
        try:
            conn.select(self.config.mailbox)
            typ, data = conn.search(None, "UNSEEN")
            if typ != "OK":
                return out
            for uid in (data[0] or b"").split():
                # Peek so the server doesn't auto-set \Seen before we commit.
                typ, msg_data = conn.fetch(uid, "(BODY.PEEK[])")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                out.append((uid.decode() if isinstance(uid, bytes) else str(uid), raw))
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
        return out

    def _mark_seen_blocking(self, uid: str, xoauth2: Optional[str] = None) -> None:
        conn = self._connect(xoauth2)
        try:
            conn.select(self.config.mailbox)
            conn.store(uid, "+FLAGS", "\\Seen")
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass

    async def fetch_unseen(self) -> List[Tuple[str, bytes]]:
        try:
            token = await self._xoauth2_string()
            return await asyncio.to_thread(self._fetch_unseen_blocking, token)
        except Exception as e:  # noqa: BLE001
            logger.warning("IMAP fetch failed for %s: %s", self.config.host, e)
            return []

    async def mark_seen(self, uid: str) -> None:
        try:
            token = await self._xoauth2_string()
            await asyncio.to_thread(self._mark_seen_blocking, uid, token)
        except Exception as e:  # noqa: BLE001
            logger.warning("IMAP mark_seen failed for uid %s: %s", uid, e)

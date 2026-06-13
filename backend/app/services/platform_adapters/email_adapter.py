"""Email platform adapter.

Mirrors :class:`SlackAdapter` / :class:`WhatsAppAdapter` so the
``ExternalPlatformManager`` and the step-notification pipeline can drive email
unchanged. The transport pieces live in ``app.services.email.*``; this class
adapts them to the ``PlatformAdapter`` contract.

Capability tiers (one integration row, derived from which creds are present):

  * SMTP only            -> SEND capability; used as the org's outbound mail
                            transport. Not a conversational channel.
  * SMTP + IMAP          -> SEND + RECEIVE; the analyst becomes an email contact.

Threading: the conversation root is the ``Message-ID`` of the first message in
the thread (user's or agent's). It is stamped onto
``Completion.external_thread_ts``; every outbound message sets ``In-Reply-To``
and ``References`` to that root so mail clients chain the thread and inbound
replies map back to the same report.
"""
from __future__ import annotations

import logging
import mimetypes
import os
from email import message_from_bytes
from email.message import Message
from email.utils import parseaddr
from typing import Any, Dict, List, Optional

from .base_adapter import PlatformAdapter
from app.services.email import security
from app.services.email.message_builder import build_email, make_message_id
from app.services.email.sender import SmtpConfig, send_message
from app.settings.config import settings

logger = logging.getLogger(__name__)

# Lines at/after these markers are quoted history we strip from inbound bodies.
_QUOTE_MARKERS = (
    "-----original message-----",
    "________________________________",
)

# Inbound attachment limits (overridable via env).
_MAX_ATTACHMENT_BYTES = int(os.environ.get("BOW_EMAIL_MAX_ATTACHMENT_BYTES", str(10 * 1024 * 1024)))  # 10 MB/file
_MAX_ATTACHMENTS = int(os.environ.get("BOW_EMAIL_MAX_ATTACHMENTS", "10"))


class EmailAdapter(PlatformAdapter):
    """IMAP/SMTP email adapter."""

    def __init__(self, platform):
        super().__init__(platform)

    # ---------- config helpers ----------

    def _from_address(self) -> Optional[str]:
        return (
            (self.credentials or {}).get("from_address")
            or (self.config or {}).get("from_address")
            or (self.credentials or {}).get("smtp_username")
        )

    def _from_name(self) -> Optional[str]:
        return (self.config or {}).get("from_name") or "Bag of words Analyst"

    def _smtp_config(self) -> SmtpConfig:
        return SmtpConfig.from_credentials(self.credentials, self.config)

    def _domain(self) -> Optional[str]:
        addr = self._from_address() or ""
        return addr.rsplit("@", 1)[1] if "@" in addr else None

    def allowed_domains(self) -> List[str]:
        return list((self.config or {}).get("allowed_domains") or [])

    # ---------- inbound parsing ----------

    @staticmethod
    def _extract_plaintext(msg: Message) -> str:
        """Return the best-effort plaintext body."""
        if msg.is_multipart():
            # Prefer the first text/plain part that isn't an attachment.
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and "attachment" not in str(
                    part.get("Content-Disposition", "")
                ):
                    payload = part.get_payload(decode=True)
                    if payload is not None:
                        return payload.decode(part.get_content_charset() or "utf-8", "replace")
            return ""
        payload = msg.get_payload(decode=True)
        if payload is None:
            return msg.get_payload() or ""
        return payload.decode(msg.get_content_charset() or "utf-8", "replace")

    @staticmethod
    def _strip_quoted(text: str) -> str:
        """Remove quoted reply history and trailing signatures."""
        lines = text.replace("\r\n", "\n").split("\n")
        kept: List[str] = []
        for line in lines:
            low = line.strip().lower()
            # "On <date>, <name> wrote:" reply attribution.
            if low.startswith("on ") and low.endswith("wrote:"):
                break
            if any(low.startswith(m) for m in _QUOTE_MARKERS):
                break
            # Signature delimiter per RFC 3676.
            if line.rstrip() == "--":
                break
            kept.append(line)
        # Drop leading '>' quote blocks and collapse trailing whitespace.
        body_lines = [ln for ln in kept if not ln.lstrip().startswith(">")]
        return "\n".join(body_lines).strip()

    @staticmethod
    def _references_root(msg: Message) -> Optional[str]:
        refs = msg.get("References")
        if refs:
            tokens = refs.split()
            if tokens:
                return tokens[0].strip()
        in_reply_to = msg.get("In-Reply-To")
        if in_reply_to:
            return in_reply_to.strip()
        return None

    async def process_incoming_message(self, event_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Parse a raw RFC822 message into the manager's processed-data shape.

        ``event_data`` is ``{"raw": <bytes>, "uid": <str>}`` as produced by the
        poller. Returns ``None`` for messages with no usable sender/body.
        """
        raw = event_data.get("raw")
        if raw is None:
            return None
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "replace")

        msg = message_from_bytes(raw)
        _name, from_address = parseaddr(msg.get("From", ""))
        from_address = (from_address or "").strip().lower()
        if not from_address:
            return None

        message_id = (msg.get("Message-ID") or make_message_id(self._domain())).strip()
        root = self._references_root(msg)
        is_thread_reply = root is not None
        effective_thread = root if is_thread_reply else message_id

        body = self._strip_quoted(self._extract_plaintext(msg))
        auth = security.parse_authentication_results(msg)
        attachments, attachments_skipped = self._extract_attachments(msg)

        return {
            "platform_type": "email",
            "external_user_id": from_address,
            "external_email": from_address,
            "external_message_id": message_id,
            # The sender address *is* the DM address; no separate channel.
            "channel_id": from_address,
            "channel_type": "im",
            "message_text": body,
            "message_type": "message",
            "timestamp": msg.get("Date"),
            "subject": (msg.get("Subject") or "").strip(),
            "from_domain": security.domain_of(from_address),
            "auth_results": auth,
            # Inbound attachments (within size/count limits) + names skipped for size.
            "attachments": attachments,
            "attachments_skipped": attachments_skipped,
            # Thread context (root id powers report re-attachment).
            "thread_ts": effective_thread,
            "message_ts": message_id,
            "is_thread_reply": is_thread_reply,
        }

    @staticmethod
    def _extract_attachments(msg: Message):
        """Pull attachment parts as ``[{filename, content, content_type, size}]``.

        Body parts (text/plain, text/html that aren't dispositioned as
        attachments) are skipped. Parts over ``_MAX_ATTACHMENT_BYTES`` or beyond
        ``_MAX_ATTACHMENTS`` are dropped and reported in the skipped list.
        """
        out = []
        skipped = []
        if not msg.is_multipart():
            return out, skipped
        for part in msg.walk():
            if part.is_multipart():
                continue
            disp = str(part.get("Content-Disposition") or "").lower()
            ctype = (part.get_content_type() or "application/octet-stream").lower()
            filename = part.get_filename()
            is_attachment = "attachment" in disp or bool(filename)
            # Skip the message body parts (not dispositioned as attachments).
            if not is_attachment:
                continue
            if ctype in ("text/plain", "text/html") and "attachment" not in disp:
                continue
            try:
                payload = part.get_payload(decode=True)
            except Exception:  # noqa: BLE001
                payload = None
            if not payload:
                continue
            size = len(payload)
            name = os.path.basename(filename or f"attachment-{len(out) + 1}")
            if size > _MAX_ATTACHMENT_BYTES or len(out) >= _MAX_ATTACHMENTS:
                skipped.append({"filename": name, "size": size})
                continue
            out.append({"filename": name, "content": payload, "content_type": ctype, "size": size})
        return out, skipped

    # ---------- outbound ----------

    async def _send(
        self,
        to_address: str,
        body: str,
        *,
        subject: Optional[str] = None,
        thread_ts: Optional[str] = None,
        message_id: Optional[str] = None,
        attachments: Optional[List] = None,
    ) -> Optional[str]:
        """Build and send a message. Returns its Message-ID on success."""
        from_address = self._from_address()
        if not from_address or not to_address:
            logger.warning("EMAIL_ADAPTER: missing from/to address")
            return None
        msg_id = message_id or make_message_id(self._domain())
        references = [thread_ts] if thread_ts else None
        msg = build_email(
            from_address=from_address,
            from_name=self._from_name(),
            to_address=to_address,
            subject=subject,
            body=body,
            message_id=msg_id,
            in_reply_to=thread_ts,
            references=references,
            attachments=attachments,
        )
        ok = await send_message(self._smtp_config(), msg)
        return msg_id if ok else None

    async def send_response(self, message_data: Dict[str, Any]) -> bool:
        to_address = (
            message_data.get("channel")
            or message_data.get("channel_id")
            or message_data.get("to")
        )
        body = message_data.get("content") or message_data.get("text") or ""
        if not to_address or not body:
            logger.warning("EMAIL_ADAPTER: send_response missing 'to' or body")
            return False
        msg_id = await self._send(
            to_address,
            body,
            subject=message_data.get("subject"),
            thread_ts=message_data.get("thread_ts"),
        )
        return msg_id is not None

    async def send_dm(self, user_id: str, text: str) -> bool:
        return await self.send_response({"to": user_id, "text": text})

    async def send_dm_in_thread(
        self,
        user_id: str,
        text: str,
        thread_ts: str = None,
        channel_id: str = None,
    ) -> bool:
        to_address = channel_id or user_id
        return await self.send_response({"to": to_address, "text": text, "thread_ts": thread_ts})

    async def send_file_in_thread(
        self,
        user_id: str,
        file_path: str,
        title: str,
        thread_ts: str = None,
        channel_id: str = None,
    ) -> bool:
        to_address = channel_id or user_id
        if not os.path.exists(file_path):
            logger.warning("EMAIL_ADAPTER: file not found at %s", file_path)
            return False
        with open(file_path, "rb") as f:
            content = f.read()
        mime, _ = mimetypes.guess_type(file_path)
        attachment = (os.path.basename(file_path), content, mime or "application/octet-stream")
        msg_id = await self._send(
            to_address,
            title or "Please find the attached results.",
            subject=title,
            thread_ts=thread_ts,
            attachments=[attachment],
        )
        return msg_id is not None

    async def send_image_in_dm(self, user_id: str, image_path: str, title: str) -> bool:
        return await self.send_file_in_thread(user_id, image_path, title)

    async def send_file_in_dm(self, user_id: str, file_path: str, title: str) -> bool:
        return await self.send_file_in_thread(user_id, file_path, title)

    async def send_new_message(
        self, to_address: str, subject: str, body: str
    ) -> Optional[str]:
        """Agent-initiated first email. Returns the Message-ID (thread root).

        The caller stamps the returned id onto the report's completion as
        ``external_thread_ts`` so the user's reply re-attaches to that report.
        """
        return await self._send(to_address, body, subject=subject)

    # ---------- identity / interface bits ----------

    async def get_user_info(
        self, external_user_id: str, conversation_id: Optional[str] = None
    ) -> Dict[str, Any]:
        # For email the external_user_id *is* the verified address.
        return {
            "id": external_user_id,
            "name": None,
            "email": external_user_id,
            "real_name": None,
        }

    async def verify_webhook_signature(
        self, request_body: bytes, signature: str, timestamp: str
    ) -> bool:
        # Inbound arrives via authenticated IMAP, not a public webhook.
        return True

    async def add_reaction(self, channel_id: str, timestamp: str, emoji: str) -> bool:
        # No email equivalent of a reaction; acknowledged as a no-op.
        return True

    async def remove_reaction(self, channel_id: str, timestamp: str, emoji: str) -> bool:
        return True

    async def send_verification_message(self, channel_id: str, email: str, token: str) -> bool:
        base_url = settings.bow_config.base_url
        verification_url = f"{base_url}/settings/integrations/verify/{token}"
        body = (
            "Account Verification Required\n\n"
            "You emailed the Bag of words analyst. To start using it, confirm "
            "your account by opening this link while signed in to Bag of words:\n\n"
            f"{verification_url}\n\n"
            "If you weren't expecting this, you can ignore this email."
        )
        msg_id = await self._send(channel_id, body, subject="Verify your Bag of words account")
        return msg_id is not None

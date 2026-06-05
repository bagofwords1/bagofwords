"""RFC 5322 message construction with conversation threading.

Email threading is driven primarily by the ``Message-ID`` / ``In-Reply-To`` /
``References`` headers, not the subject. Every outbound message we send gets a
stable ``Message-ID``; replies carry the conversation root in ``In-Reply-To``
and ``References`` so that:

  * mail clients chain the messages into one visible thread, and
  * when the user replies, we can map their message back to the originating
    BOW report by matching the root id against ``Completion.external_thread_ts``.

The "root id" is the ``Message-ID`` of the first message in the conversation —
whether that first message came from the user (they emailed the analyst) or
from the agent (the analyst emailed them first). That symmetry is what lets an
agent-initiated email and the user's reply land on the same report.
"""
from __future__ import annotations

import os
import uuid
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import List, Optional, Sequence, Tuple


def make_message_id(domain: Optional[str] = None) -> str:
    """Generate a globally-unique Message-ID, optionally scoped to ``domain``."""
    if domain:
        return make_msgid(domain=domain)
    return make_msgid()


def _normalize_subject(subject: Optional[str], is_reply: bool) -> str:
    subject = (subject or "").strip() or "Bag of words"
    if is_reply and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    return subject


def build_email(
    *,
    from_address: str,
    from_name: Optional[str],
    to_address: str,
    subject: Optional[str],
    body: str,
    body_subtype: str = "plain",
    message_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[Sequence[str]] = None,
    attachments: Optional[List[Tuple[str, bytes, str]]] = None,
) -> EmailMessage:
    """Construct an ``EmailMessage`` with threading + optional attachments.

    ``attachments`` is a list of ``(filename, content_bytes, mime_type)``.
    ``references`` is the ordered list of ancestor Message-IDs; the conversation
    root should be first. When ``in_reply_to`` is set the subject is prefixed
    with ``Re:`` if it isn't already.
    """
    msg = EmailMessage()
    msg["Message-ID"] = message_id or make_message_id()
    msg["Date"] = formatdate(localtime=True)
    if from_name:
        msg["From"] = f"{from_name} <{from_address}>"
    else:
        msg["From"] = from_address
    msg["To"] = to_address
    msg["Subject"] = _normalize_subject(subject, is_reply=bool(in_reply_to or references))

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    refs = list(references or [])
    if in_reply_to and in_reply_to not in refs:
        refs.append(in_reply_to)
    if refs:
        msg["References"] = " ".join(refs)

    # Mark our own mail so the inbound poller can suppress loops if a copy ever
    # comes back to the analyst mailbox.
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-BOW-Mailer"] = "bagofwords-analyst"

    msg.set_content(body, subtype=body_subtype)

    for filename, content, mime_type in attachments or []:
        maintype, _, subtype = (mime_type or "application/octet-stream").partition("/")
        msg.add_attachment(
            content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=filename,
        )

    return msg

"""Email files (.eml RFC 822, .msg Outlook) as readable, searchable documents.

An email is a document with sub-files: the message itself (headers + body)
plus its attachments. This module turns both container formats into ONE
model-facing shape:

    From: …
    To: …
    Cc: …
    Date: …
    Subject: …

    Attachments (2):
      [0] report.pdf (12.3 KB) — file_id: <this-email>#att:0:report.pdf
      [1] data.csv (120 B) — file_id: <this-email>#att:1:data.csv

    <body — text/plain preferred, HTML stripped to text otherwise>

    --- Attachment [0] report.pdf ---
    <extracted text of the attachment, so search_files finds terms inside it>
    …

`.msg` is converted to a standard ``EmailMessage`` from python-oxmsg's typed
MAPI properties (``_convert_msg``) and then rendered by the same code as
`.eml`, so the two formats cannot drift apart.

Attachments are addressed as sub-files of the email they live in:
``<parent file id>#att:<index>:<filename>`` — the 0-based index makes the
reference unambiguous (two attachments may share a name), the filename is for
humans. A nested message (a forwarded .eml / embedded .msg) is itself an
attachment; its own attachments chain another ``#att:`` segment on top, up
to ``EMAIL_MAX_DEPTH`` levels. The chain is only ever resolved by first
reading the PARENT through the connector's normal path, so whatever scope /
permission check protects the email protects its attachments.

Every public function here is defensive: a corrupt message, an undecodable
part or a missing optional library yields a shorter (or empty) rendering,
never an exception — one bad file must not break a directory search.
"""
from __future__ import annotations

import email
import email.header
import logging
import mimetypes
import os
import re
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage, Message

logger = logging.getLogger(__name__)

# Extensions this module turns into text. `_document_text.DOC_EXTS` includes
# them so every connector's "rich document" branch picks them up.
EMAIL_EXTS = {"eml", "msg"}

# How many levels of nested messages are rendered / addressable: the email
# (0) → a forwarded message inside it (1) → a message inside that (2). Deeper
# nesting is listed but not expanded, and ids with a longer chain are refused.
EMAIL_MAX_DEPTH = 3

# Sub-file id grammar: `<parent>#att:<index>:<name>` repeated per level.
ATTACHMENT_ID_SEP = "#att:"
_ATTACHMENT_SEG_RE = re.compile(r"#att:(\d+):")

# Placeholder the renderer writes in place of the email's own file id (the
# extractor only knows the file's NAME; the id is a connector concept). The
# read_file tool substitutes the real id before the text reaches the model.
EMAIL_ID_PLACEHOLDER = "<this-email>"

# The .msg MIME type is not in Python's table, so listings reported `None`
# for Outlook files while `.eml` got message/rfc822. Register once, globally.
mimetypes.add_type("application/vnd.ms-outlook", ".msg")
mimetypes.add_type("message/rfc822", ".eml")

# Minimum room kept for the attachment list when a long body would otherwise
# fill the whole character budget — the list is what makes attachments
# reachable, so it must survive truncation.
_ATTACHMENT_LIST_RESERVE = 2_000


def _ext(name: str) -> str:
    leaf = str(name or "").rsplit("/", 1)[-1]
    return leaf.rsplit(".", 1)[-1].lower() if "." in leaf else ""


def is_email_name(name: str) -> bool:
    return _ext(name) in EMAIL_EXTS


# ----------------------------------------------------------------- ids


def split_attachment_id(file_id: str) -> tuple[str, list[tuple[int, str]]]:
    """``'a/b.eml#att:2:fwd.eml#att:0:x.pdf'`` → ``('a/b.eml', [(2, 'fwd.eml'), (0, 'x.pdf')])``.

    A plain file id comes back as ``(file_id, [])``. Only the strict
    ``#att:<digits>:`` form is recognised, so a `#` in an ordinary filename
    does not turn it into an attachment reference."""
    fid = str(file_id or "")
    m = _ATTACHMENT_SEG_RE.search(fid)
    if not m:
        return fid, []
    parent = fid[: m.start()]
    chain: list[tuple[int, str]] = []
    pos = m.start()
    while pos < len(fid):
        m = _ATTACHMENT_SEG_RE.match(fid, pos)
        if not m:
            break
        nxt = _ATTACHMENT_SEG_RE.search(fid, m.end())
        name = fid[m.end(): nxt.start() if nxt else len(fid)]
        chain.append((int(m.group(1)), name))
        pos = nxt.start() if nxt else len(fid)
    return parent, chain


def attachment_id(parent_id: str, index: int, name: str) -> str:
    return f"{parent_id}{ATTACHMENT_ID_SEP}{index}:{name}"


def absolutize_attachment_ids(text: str, file_id: str) -> str:
    """Replace the renderer's placeholder with the email's real file id."""
    if not text or EMAIL_ID_PLACEHOLDER not in text:
        return text
    return text.replace(EMAIL_ID_PLACEHOLDER, str(file_id or ""))


# ------------------------------------------------------------- parsing


@dataclass
class EmailAttachment:
    index: int
    name: str
    size: int
    mime: str
    data: bytes
    # True for a nested message (message/rfc822 part or an embedded .msg):
    # its bytes are themselves an email, readable/renderable as one.
    is_message: bool = False


def parse_email_bytes(data: bytes, name: str) -> EmailMessage | None:
    """Parse `.eml` (stdlib, modern policy) or `.msg` (python-oxmsg →
    ``_convert_msg``) into an ``EmailMessage``. None when unparseable."""
    try:
        if _ext(name) == "msg":
            return _msg_to_email_message(data)
        return email.message_from_bytes(data, policy=policy.default)
    except Exception as e:
        logger.debug("parse_email_bytes failed for %s: %s", name, e)
        return None


def _msg_to_email_message(data: bytes) -> EmailMessage | None:
    """Outlook MSG → EmailMessage, built from python-oxmsg's typed properties.

    Building the MIME tree here (instead of trusting a transport-header
    block, which every locally composed Outlook message lacks) keeps
    subject/sender/recipients/date, the plain and HTML bodies, by-value
    attachments and embedded messages (as proper message/rfc822 parts, so
    the `.eml` walker treats both formats alike)."""
    from oxmsg import Message as OxMessage

    return _convert_msg(OxMessage.load(data), 0)


# MS-OXMSG / MS-OXPROPS identifiers python-oxmsg does not surface itself.
_PID_RECIPIENT_TYPE = 0x0C15      # 1 = To, 2 = Cc, 3 = Bcc
_PID_ATTACH_METHOD = 0x3705       # 5 = embedded message
_PID_RTF_COMPRESSED = 0x1009
_EMBEDDED_MSG_STORAGE = "__substg1.0_3701000D"
_EMBEDDED_HDR_OFFSET = 24         # top-level messages use 32


def _embedded_message(storage):
    """A python-oxmsg Message over an embedded-message storage. Its property
    stream has the 24-byte header of an embedded message, not the 32-byte
    header of a top-level one, which `Message` assumes."""
    from oxmsg.message import Message as OxMessage
    from oxmsg.properties import Properties

    class _Embedded(OxMessage):
        @property
        def properties(self):
            return Properties(self._storage, properties_header_offset=_EMBEDDED_HDR_OFFSET)

        _properties = properties

    return _Embedded(storage)


def _recipient_header(recipient) -> str:
    name = (getattr(recipient, "name", None) or "").strip()
    addr = (getattr(recipient, "email_address", None) or "").strip()
    if name and addr and name != addr:
        return f'"{name}" <{addr}>'
    return addr or name


def _rtf_to_text(rtf: str) -> str:
    r"""Best-effort plain text from an RTF body: control words and groups
    dropped, paragraph marks kept as newlines, ``\'xx`` and ``\uN`` escapes
    decoded. Encapsulated HTML (``\fromhtml``) keeps its text; its tags sit
    in ``{\*\htmltag ...}`` destination groups, which are skipped."""
    out: list[str] = []
    skip_depth: list[bool] = [False]
    i, n = 0, len(rtf)
    uc_skip = 0
    while i < n:
        ch = rtf[i]
        if ch == "{":
            skip_depth.append(skip_depth[-1])
            i += 1
            continue
        if ch == "}":
            if len(skip_depth) > 1:
                skip_depth.pop()
            i += 1
            continue
        if ch == "\\":
            m = re.match(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\(.)", rtf[i:])
            if not m:
                i += 1
                continue
            i += m.end()
            word, arg, hexv, sym = m.group(1), m.group(2), m.group(3), m.group(4)
            if skip_depth[-1]:
                continue
            if hexv is not None:
                if uc_skip:
                    uc_skip -= 1
                else:
                    out.append(bytes([int(hexv, 16)]).decode("cp1252", "replace"))
            elif sym is not None:
                if sym == "*":
                    skip_depth[-1] = True
                elif sym in "\\{}":
                    out.append(sym)
                elif sym == "~":
                    out.append(" ")
            elif word in ("par", "line", "row"):
                out.append("\n")
            elif word == "tab":
                out.append("\t")
            elif word == "u" and arg is not None:
                cp = int(arg)
                out.append(chr(cp + 65536 if cp < 0 else cp))
                uc_skip = 1
            elif word in ("fonttbl", "colortbl", "stylesheet", "info", "pict", "htmlrtf"):
                if word == "htmlrtf" and arg == "0":
                    continue
                skip_depth[-1] = True
            continue
        if ch in "\r\n":
            i += 1
            continue
        if not skip_depth[-1]:
            if uc_skip:
                uc_skip -= 1
            else:
                out.append(ch)
        i += 1
    text = "".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _rtf_body(msg) -> str:
    """Text of the compressed RTF body, for messages with no plain or HTML
    body (older Outlook clients). Empty when absent or undecodable."""
    try:
        raw = msg.properties.binary_prop_value(_PID_RTF_COMPRESSED)
        if not raw:
            return ""
        from compressed_rtf import decompress

        return _rtf_to_text(decompress(raw).decode("cp1252", "replace"))
    except Exception as e:
        logger.debug("msg RTF body skipped: %s", e)
        return ""


def _convert_msg(msg, depth: int) -> EmailMessage:
    import email.utils

    em = EmailMessage(policy=policy.default)

    def _safe(fn):
        try:
            return fn()
        except Exception:
            return None

    sender = _safe(lambda: msg.sender)
    if sender:
        em["From"] = str(sender)
    to, cc = [], []
    for r in _safe(lambda: msg.recipients) or ():
        try:
            kind = r.properties.int_prop_value(_PID_RECIPIENT_TYPE)
        except Exception:
            kind = None
        value = _recipient_header(r)
        if not value or kind == 3:  # Bcc is not a visible header
            continue
        (cc if kind == 2 else to).append(value)
    if to:
        em["To"] = ", ".join(to)
    if cc:
        em["Cc"] = ", ".join(cc)
    subject = _safe(lambda: msg.subject)
    if subject:
        em["Subject"] = str(subject)
    date = _safe(lambda: msg.sent_date)
    if date:
        em["Date"] = email.utils.format_datetime(date) if getattr(date, "tzinfo", None) else str(date)

    body = _safe(lambda: msg.body) or ""
    html = _safe(lambda: msg.html_body)
    if isinstance(html, bytes):
        html = html.decode("utf-8", "replace")
    if not body and not html:
        body = _rtf_body(msg)
    if body:
        em.set_content(str(body))
        if html:
            em.add_alternative(str(html), subtype="html")
    elif html:
        em.set_content(str(html), subtype="html")
    else:
        em.set_content("")

    for i, att in enumerate(_safe(lambda: msg.attachments) or ()):
        try:
            name = att.file_name or f"attachment-{i}"
            embedded = None
            if att.properties.int_prop_value(_PID_ATTACH_METHOD) == 5:
                embedded = next((s for s in att._storage.storages if s.name == _EMBEDDED_MSG_STORAGE), None)
            if embedded is not None:
                # Embedded message: converted recursively. Depth is bounded
                # by the renderer / id grammar anyway, but a pathological
                # file must not recurse without limit.
                if depth + 1 > EMAIL_MAX_DEPTH:
                    continue
                inner = _convert_msg(_embedded_message(embedded), depth + 1)
                if name.lower().endswith(".msg"):
                    name = name[:-4] + ".eml"
                elif not name.lower().endswith(".eml"):
                    name += ".eml"
                em.add_attachment(inner, filename=name)
                continue
            payload = att.file_bytes
            if payload is None:
                continue
            mime = (att.mime_type if att.mime_type and att.mime_type != "application/octet-stream" else None) \
                or mimetypes.guess_type(name)[0] or "application/octet-stream"
            maintype, _, subtype = str(mime).partition("/")
            em.add_attachment(bytes(payload), maintype=maintype or "application",
                              subtype=subtype or "octet-stream", filename=name)
        except Exception as e:
            logger.debug("msg attachment %s skipped: %s", i, e)
    return em


def _decode_header_value(raw) -> str:
    """Decoded, single-line header text. The modern policy already hands back
    Unicode; this is the belt for the malformed cases (bad RFC 2047 words, a
    raw legacy charset) where attribute access raises or returns bytes."""
    if raw is None:
        return ""
    try:
        parts = email.header.decode_header(str(raw))
        text = str(email.header.make_header(parts))
    except Exception:
        try:
            text = str(raw)
        except Exception:
            return ""
    return re.sub(r"\s*[\r\n]+\s*", " ", text).strip()


def _header(msg: Message, name: str) -> str:
    try:
        value = msg.get(name)
    except Exception:
        # policy.default can raise while parsing a hopelessly malformed
        # header; the raw compat32 string is still better than nothing.
        try:
            value = msg.get_all(name)
            value = value[0] if value else None
        except Exception:
            value = None
    return _decode_header_value(value)


def _part_text(part: Message) -> str:
    """Decoded text of a text/* part, charset-aware, never raising."""
    try:
        content = part.get_content()
        if isinstance(content, bytes):
            content = content.decode(part.get_content_charset() or "utf-8", "replace")
        return str(content)
    except Exception:
        pass
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return str(part.get_payload() or "")
        return payload.decode(part.get_content_charset() or "utf-8", "replace")
    except Exception:
        return ""


def _safe_filename(name: str | None, fallback: str) -> str:
    leaf = os.path.basename(str(name or "").replace("\\", "/")).strip()
    # The id grammar reuses `#att:`; a name that contains it could not be
    # addressed, so neutralise it (vanishingly rare in practice).
    leaf = leaf.replace(ATTACHMENT_ID_SEP, "_att_")
    return leaf or fallback


def _part_filename(part: Message) -> str | None:
    try:
        return part.get_filename()
    except Exception:
        return None


def _part_bytes(part: Message) -> bytes:
    try:
        payload = part.get_payload(decode=True)
        if payload is not None:
            return bytes(payload)
    except Exception:
        pass
    try:
        return part.as_bytes()
    except Exception:
        return b""


def _nested_message_bytes(part: Message) -> tuple[bytes, str | None]:
    """Bytes + best name for a nested message part. A standard message/rfc822
    part wraps the inner message as its single payload; an embedded .msg
    may arrive as a multipart part that carries its own From/Subject."""
    inner = None
    try:
        if part.get_content_type() == "message/rfc822":
            payload = part.get_payload()
            inner = payload[0] if isinstance(payload, list) and payload else None
    except Exception:
        inner = None
    target = inner if inner is not None else part
    # A message/rfc822 part may itself be transfer-encoded (base64 /
    # quoted-printable — the stdlib does this when a message is attached as
    # bytes, and real mailers do too). The parser does NOT decode before
    # parsing the inner message, so `inner` is then a headerless shell whose
    # body is the encoded text. Decode the container's encoding and re-parse,
    # so the forwarded message's headers and body are real.
    try:
        cte = str(part.get("Content-Transfer-Encoding") or "").strip().lower()
    except Exception:
        cte = ""
    if inner is not None and cte in ("base64", "quoted-printable"):
        try:
            encoded = inner.get_payload()
            if isinstance(encoded, str):
                import base64
                import quopri

                raw = encoded.encode("ascii", "ignore")
                decoded = base64.b64decode(raw, validate=False) if cte == "base64" else quopri.decodestring(raw)
                if decoded.strip():
                    target = email.message_from_bytes(decoded, policy=policy.default)
        except Exception:
            pass
    try:
        data = target.as_bytes(policy=policy.default)
    except Exception:
        try:
            data = target.as_bytes()
        except Exception:
            data = b""
    subject = _header(target, "Subject")
    return data, subject or None


def _looks_like_message(part: Message) -> bool:
    try:
        if part.get_content_type() == "message/rfc822":
            return True
        return part.is_multipart() and (part.get("Subject") is not None or part.get("From") is not None)
    except Exception:
        return False


def _collect(msg: Message, plain: list[str], html: list[str], raw_atts: list) -> None:
    """Depth-first walk separating BODY text parts from attachment parts.

    Body = text/plain or text/html parts not dispositioned as attachments and
    carrying no filename. Everything else that is a leaf — files, inline
    images, calendar invites — is an attachment, as is any nested message."""
    # get_payload(), not iter_parts(): sub-parts of a converted message can be
    # compat32 Message objects (MIMEMultipart/MIMEText), which lack the
    # EmailMessage-only iterator.
    try:
        payload = msg.get_payload() if msg.is_multipart() else None
        parts = list(payload) if isinstance(payload, list) else []
    except Exception:
        parts = []
    if not parts and not msg.is_multipart():
        ctype = msg.get_content_type()
        if ctype == "text/html":
            html.append(_part_text(msg))
        elif ctype.startswith("text/"):
            plain.append(_part_text(msg))
        return
    for part in parts:
        if _looks_like_message(part):
            raw_atts.append(part)
            continue
        if part.is_multipart():
            _collect(part, plain, html, raw_atts)
            continue
        ctype = part.get_content_type()
        try:
            disp = part.get_content_disposition()
        except Exception:
            disp = None
        fname = _part_filename(part)
        if disp != "attachment" and not fname:
            if ctype == "text/plain":
                plain.append(_part_text(part))
                continue
            if ctype == "text/html":
                html.append(_part_text(part))
                continue
        raw_atts.append(part)


def email_attachments(msg: Message) -> list[EmailAttachment]:
    """Attachments of a parsed message, in document order, 0-indexed. The
    index/name pair is what the sub-file id carries, so this must be
    deterministic for a given message — same walk every time."""
    plain: list[str] = []
    html: list[str] = []
    raw: list = []
    _collect(msg, plain, html, raw)
    out: list[EmailAttachment] = []
    for i, part in enumerate(raw):
        if _looks_like_message(part):
            data, subject = _nested_message_bytes(part)
            fname = _part_filename(part)
            if fname and _ext(fname) == "msg":
                # _convert_msg re-encodes an embedded .msg as MIME, so the
                # bytes ARE an .eml now; name them so the reader agrees.
                fname = fname[:-4] + ".eml"
            name = _safe_filename(fname or (f"{subject}.eml" if subject else None), f"message-{i}.eml")
            if _ext(name) not in EMAIL_EXTS:
                name += ".eml"
            out.append(EmailAttachment(i, name, len(data), "message/rfc822", data, is_message=True))
            continue
        data = _part_bytes(part)
        mime = (part.get_content_type() or "application/octet-stream").lower()
        fname = _part_filename(part)
        if not fname:
            guess = mimetypes.guess_extension(mime) or ""
            fname = f"attachment-{i}{guess}"
        out.append(EmailAttachment(i, _safe_filename(fname, f"attachment-{i}"), len(data), mime, data))
    return out


def email_body_text(msg: Message) -> str:
    """The message body as plain text: text/plain parts when present, else
    the HTML parts stripped to text with the mail connectors' helper."""
    from app.data_sources.clients.mail_common import strip_html

    plain: list[str] = []
    html: list[str] = []
    _collect(msg, plain, html, [])
    text = "\n\n".join(p.strip() for p in plain if p and p.strip()).strip()
    if text:
        return text
    if html:
        return strip_html("\n".join(html))
    return ""


# ----------------------------------------------------------- rendering


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _attachment_text(att: EmailAttachment, max_chars: int, depth: int, prefix: str) -> str:
    """Searchable text of one attachment, bounded by max_chars. Nested
    messages render through this module (depth + 1); documents through the
    shared extractor; text/tabular files are decoded directly."""
    if max_chars <= 0 or not att.data:
        return ""
    ext = _ext(att.name)
    if att.is_message or ext in EMAIL_EXTS:
        if depth + 1 >= EMAIL_MAX_DEPTH:
            return "(nested message beyond the depth limit — not expanded)"
        return render_email_text(att.data, att.name, max_chars, depth=depth + 1, file_id=prefix)
    try:
        from app.data_sources.clients._document_text import (
            DOC_EXTS,
            doc_text_looks_garbled,
            extract_document_text_from_bytes,
        )
        if ext in DOC_EXTS:
            text = extract_document_text_from_bytes(att.data, att.name, max_chars=max_chars)
            return "" if doc_text_looks_garbled(text) else text
        if ext in ("xlsx", "xls"):
            import io

            import pandas as pd

            frames = pd.read_excel(io.BytesIO(att.data), sheet_name=None, header=None)
            parts = [f"{sheet}\n{df.to_csv(index=False, header=False)}" for sheet, df in frames.items()]
            return "\n".join(parts)[:max_chars]
        if ext in _TEXT_LIKE_EXTS or att.mime.startswith("text/"):
            text = att.data.decode("utf-8-sig", errors="replace")
            if ext in ("html", "htm") or att.mime == "text/html":
                from app.data_sources.clients.mail_common import strip_html

                text = strip_html(text)
            return text[:max_chars]
    except Exception as e:
        logger.debug("attachment text skipped for %s: %s", att.name, e)
    return ""


_TEXT_LIKE_EXTS = {
    "txt", "md", "csv", "tsv", "json", "log", "xml", "yaml", "yml", "html", "htm", "ics", "sql",
}


def render_email_text(data: bytes, name: str, max_chars: int, *, depth: int = 0,
                      file_id: str | None = None) -> str:
    """Flat text of an email file: header block, body, attachment list with
    sub-file ids, then each attachment's own text (nested messages inline).

    ``file_id`` is the id to prefix attachment ids with; the default is a
    placeholder the read tool replaces (see ``absolutize_attachment_ids``).
    Nested messages pass their own full id down so a chain like
    ``<email>#att:2:fwd.eml#att:0:x.pdf`` comes out ready to use."""
    msg = parse_email_bytes(data, name)
    if msg is None:
        return ""
    prefix = file_id if file_id is not None else EMAIL_ID_PLACEHOLDER
    lines: list[str] = []
    for label in ("From", "To", "Cc", "Date", "Subject"):
        value = _header(msg, label)
        if value or label in ("From", "Subject"):
            lines.append(f"{label}: {value}")
    header_block = "\n".join(lines)

    atts = email_attachments(msg)
    body = email_body_text(msg)
    body_cap = max_chars - len(header_block) - (_ATTACHMENT_LIST_RESERVE if atts else 0)
    if body_cap < 0:
        body_cap = 0
    if len(body) > body_cap:
        body = body[:body_cap] + "\n[body truncated]"

    # The attachment list sits BEFORE the body: the planner reads a capped
    # observation of this text, and a long body would otherwise push the
    # attachment ids — the only way to reach the attachments — out of what
    # the model sees. Attachment TEXT still follows the body.
    out = [header_block]
    if atts:
        out.append("")
        out.append(f"Attachments ({len(atts)}):")
        for a in atts:
            out.append(f"  [{a.index}] {a.name} ({_fmt_size(a.size)}) — file_id: {attachment_id(prefix, a.index, a.name)}")
    if body:
        out.extend(["", body])
    text = "\n".join(out)
    if atts:
        for a in atts:
            remaining = max_chars - len(text)
            if remaining <= 0:
                break
            sub = _attachment_text(a, remaining, depth, attachment_id(prefix, a.index, a.name))
            if sub and sub.strip():
                text += f"\n\n--- Attachment [{a.index}] {a.name} ---\n{sub.strip()}"
    return text[:max_chars]


# --------------------------------------------------- sub-file resolution


def extract_email_attachment(data: bytes, name: str, chain: list[tuple[int, str]]) -> tuple[bytes, str, str]:
    """Bytes + name + MIME of the attachment a ``#att:`` chain points at.

    Raises ValueError with a message the model can act on (lists the real
    attachments) for an unknown index, a name that no longer matches, a chain
    deeper than ``EMAIL_MAX_DEPTH`` or a level that is not itself an email.
    """
    if not chain:
        raise ValueError("Not an attachment id.")
    if len(chain) > EMAIL_MAX_DEPTH:
        raise ValueError(
            f"Attachment references may nest at most {EMAIL_MAX_DEPTH} level(s) deep."
        )
    current, current_name, current_mime = bytes(data), name, mimetypes.guess_type(name)[0] or ""
    for index, want_name in chain:
        if not is_email_name(current_name):
            raise ValueError(
                f"'{current_name}' is not an email file — only .eml/.msg files have attachments."
            )
        msg = parse_email_bytes(current, current_name)
        if msg is None:
            raise ValueError(f"'{current_name}' could not be parsed as an email.")
        atts = email_attachments(msg)
        if index < 0 or index >= len(atts):
            listing = ", ".join(f"[{a.index}] {a.name}" for a in atts) or "none"
            raise ValueError(
                f"'{current_name}' has no attachment #{index}. Attachments: {listing}."
            )
        att = atts[index]
        if want_name and att.name.casefold() != want_name.casefold():
            raise ValueError(
                f"Attachment #{index} of '{current_name}' is '{att.name}', not '{want_name}' "
                f"— the email may have changed; re-read it to get current attachment ids."
            )
        current, current_name, current_mime = att.data, att.name, att.mime
    return current, current_name, current_mime

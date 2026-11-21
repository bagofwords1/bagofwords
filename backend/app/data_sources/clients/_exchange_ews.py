"""Read-only Exchange Web Services (EWS) wire layer.

Builds the handful of SOAP requests the on-premises Exchange mail connector
needs, sends them over a Windows-authenticated session, and turns responses
into plain dicts. Only read operations exist here: GetFolder, FindFolder,
FindItem and GetItem. There is deliberately no code that sends, moves, flags or
deletes anything.

Responses are parsed without DTDs or entity expansion and are size-bounded
before parsing.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from typing import Any, Dict, List, Optional, Sequence
from xml.etree import ElementTree as ET

import requests
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
NS_T = "http://schemas.microsoft.com/exchange/services/2006/types"
NS_M = "http://schemas.microsoft.com/exchange/services/2006/messages"
NS_E = "http://schemas.microsoft.com/exchange/services/2006/errors"

#: Request schema version. Exchange 2013 and every later release accept it.
REQUEST_SERVER_VERSION = "Exchange2013"

#: Ceiling for one response body. Folder and listing pages are far smaller; a
#: single message with a very large body is the case this bounds.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024

#: How long a rejected username/password is remembered. While it is, no further
#: sign-in attempt is sent for that exact credential: repeated attempts with a
#: stale password can lock the account in the directory.
REJECTED_CREDENTIAL_TTL_SECONDS = 300

#: Longest the client waits when the server asks it to back off.
MAX_BACKOFF_SECONDS = 5.0

_WELL_KNOWN = ("inbox", "sentitems", "deleteditems", "drafts", "junkemail")

# Character references to code points XML 1.0 forbids. Message bodies with
# stray control characters come back with these, and a strict parser rejects
# the whole response over one byte of noise.
_ILLEGAL_CHAR_REF = re.compile(rb"&#(?:x(?:0*(?:[0-8bcefBCEF]|1[0-9a-fA-F]))|0*(?:[0-8]|1[124-9]|2[0-9]|3[01]));")


def _t(name: str) -> str:
    return f"{{{NS_T}}}{name}"


def _m(name: str) -> str:
    return f"{{{NS_M}}}{name}"


class ExchangeError(ValueError):
    """A request Exchange refused, with the server's response code attached.

    The message is written for the person using the connection; the raw server
    text is never included because it can echo request content.
    """

    def __init__(self, code: str, message: str, *, back_off: Optional[float] = None):
        self.code = code
        self.back_off = back_off
        super().__init__(message)


#: Response codes that mean "this identity cannot see that mailbox or folder".
#: Exchange answers a folder the caller has no rights on with "not found".
ACCESS_CODES = frozenset({
    "ErrorAccessDenied", "ErrorFolderNotFound", "ErrorItemNotFound",
    "ErrorNonExistentMailbox", "ErrorMailboxStoreUnavailable",
    "ErrorMailRecipientNotFound", "ErrorImpersonateUserDenied",
})

_HINTS = {
    "ErrorAccessDenied": "This account does not have permission to open that mailbox or folder.",
    "ErrorFolderNotFound": "The folder was not found, or this account has no permission to see it.",
    "ErrorItemNotFound": "The message no longer exists; it was moved or deleted since it was listed.",
    "ErrorNonExistentMailbox": "No mailbox exists for that address on this Exchange server.",
    "ErrorMailRecipientNotFound": "No mailbox exists for that address on this Exchange server.",
    "ErrorMailboxStoreUnavailable": "The mailbox database is unavailable right now. Try again later.",
    "ErrorMailboxMoveInProgress": "The mailbox is being moved. Try again later.",
    "ErrorServerBusy": "The Exchange server is throttling requests. Try again in a moment.",
    "ErrorInvalidIdMalformed": "That id is not a valid Exchange id. Use the id exactly as it was returned.",
    "ErrorInvalidId": "That id is not a valid Exchange id. Use the id exactly as it was returned.",
    "ErrorInvalidServerVersion": "This Exchange server is older than the versions this connector supports (2013 and later).",
}


def error_for(code: str, *, back_off: Optional[float] = None) -> ExchangeError:
    hint = _HINTS.get(code, "Exchange could not complete the request.")
    return ExchangeError(code, f"{hint} ({code})", back_off=back_off)


# ---------------------------------------------------------------- lockout guard

_rejected: Dict[str, float] = {}
_rejected_lock = threading.Lock()


def _credential_key(url: str, username: Optional[str], password: Optional[str]) -> Optional[str]:
    if not username or not password:
        return None  # Kerberos: nothing a retry could lock out.
    raw = "\x00".join((url, username, password)).encode()
    return hashlib.sha256(raw).hexdigest()


def _is_rejected(key: Optional[str], now: float) -> bool:
    if key is None:
        return False
    with _rejected_lock:
        until = _rejected.get(key)
        if until is None:
            return False
        if until <= now:
            del _rejected[key]
            return False
        return True


def _mark_rejected(key: Optional[str], now: float) -> None:
    if key is None:
        return
    with _rejected_lock:
        _rejected[key] = now + REJECTED_CREDENTIAL_TTL_SECONDS
        for stale in [k for k, until in _rejected.items() if until <= now]:
            del _rejected[stale]


def forget_rejected_credentials() -> None:
    """Clear the rejected-credential memory (used by tests)."""
    with _rejected_lock:
        _rejected.clear()


_SIGN_IN_AGAIN = (
    "Exchange rejected the username or password. Update the credentials for this "
    "connection and try again."
)


# ------------------------------------------------------------------- transport

class EwsTransport:
    """Sends one SOAP request and returns its response messages."""

    def __init__(
        self,
        url: str,
        sessions,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self.url = url
        self._sessions = sessions
        self._key = _credential_key(url, username, password)

    def call(self, operation: ET.Element) -> List[ET.Element]:
        """Run one operation; returns its ``*ResponseMessage`` elements.

        A message carrying an error is returned as-is — callers that read
        several folders at once decide which failures to tolerate. Use
        :func:`ensure_success` when any failure should raise.
        """
        payload = self._envelope(operation)
        for attempt in range(2):
            try:
                return self._send(payload)
            except ExchangeError as exc:
                if exc.code != "ErrorServerBusy" or attempt:
                    raise
                time.sleep(min(exc.back_off or 1.0, MAX_BACKOFF_SECONDS))
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _envelope(operation: ET.Element) -> bytes:
        envelope = ET.Element(f"{{{NS_SOAP}}}Envelope")
        header = ET.SubElement(envelope, f"{{{NS_SOAP}}}Header")
        ET.SubElement(header, _t("RequestServerVersion"), {"Version": REQUEST_SERVER_VERSION})
        ET.SubElement(envelope, f"{{{NS_SOAP}}}Body").append(operation)
        return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)

    def _send(self, payload: bytes) -> List[ET.Element]:
        if _is_rejected(self._key, time.monotonic()):
            raise ExchangeError("Unauthorized", _SIGN_IN_AGAIN)
        for attempt in range(2):
            try:
                session = self._sessions.get()
                with session.post(
                    self.url, data=payload,
                    headers={"Content-Type": "text/xml; charset=utf-8", "Accept": "text/xml"},
                    timeout=(10, 60), allow_redirects=False, stream=True,
                ) as response:
                    status = response.status_code
                    if status == 401:
                        # One refusal is final. Never retry a sign-in.
                        _mark_rejected(self._key, time.monotonic())
                        raise ExchangeError("Unauthorized", _SIGN_IN_AGAIN)
                    if 300 <= status < 400:
                        raise ExchangeError(
                            "Redirected",
                            "Exchange redirected the request. Enter the server's own Exchange Web Services URL.",
                        )
                    if status == 403:
                        raise ExchangeError("Forbidden", "This account is not allowed to use Exchange Web Services.")
                    if status == 404:
                        raise ExchangeError("NotFound", "This URL is not an Exchange Web Services endpoint.")
                    if status == 503:
                        raise ExchangeError("ErrorServerBusy", _HINTS["ErrorServerBusy"])
                    if status not in (200, 500):
                        raise ExchangeError("HttpError", f"Exchange request failed (HTTP {status}).")
                    body = self._read(response)
                return self._parse(body)
            except ExchangeError:
                raise
            except requests.exceptions.SSLError as exc:
                raise ExchangeError(
                    "TlsError",
                    "TLS verification failed. Install the issuing certificate authority in the backend trust store.",
                ) from exc
            except requests.RequestException as exc:
                self._sessions.reset()
                if attempt:
                    raise ExchangeError(
                        "Unreachable",
                        "Cannot reach the Exchange server. Check the URL, DNS, network access and server availability.",
                    ) from exc
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _read(response) -> bytes:
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
            raise ExchangeError("TooLarge", "The Exchange response is too large to read.")
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ExchangeError("TooLarge", "The Exchange response is too large to read.")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _parse(body: bytes) -> List[ET.Element]:
        try:
            try:
                root = fromstring(body)
            except ET.ParseError:
                root = fromstring(_ILLEGAL_CHAR_REF.sub(b"", body))
        except (ET.ParseError, DefusedXmlException, ValueError) as exc:
            raise ExchangeError("NotFound", "This URL is not an Exchange Web Services endpoint.") from exc
        soap_body = root.find(f"{{{NS_SOAP}}}Body")
        if root.tag != f"{{{NS_SOAP}}}Envelope" or soap_body is None:
            raise ExchangeError("NotFound", "This URL is not an Exchange Web Services endpoint.")
        fault = soap_body.find(f"{{{NS_SOAP}}}Fault")
        if fault is not None:
            code = (fault.findtext(f"detail/{{{NS_E}}}ResponseCode") or "").strip() or "SoapFault"
            raise error_for(code, back_off=_back_off(fault))
        messages = soap_body.findall(f"*/{_m('ResponseMessages')}/*")
        if not messages:
            raise ExchangeError("NotFound", "This URL is not an Exchange Web Services endpoint.")
        for message in messages:
            if message.findtext(_m("ResponseCode")) == "ErrorServerBusy":
                raise error_for("ErrorServerBusy", back_off=_back_off(message))
        return messages


def _back_off(node: ET.Element) -> Optional[float]:
    for value in node.iter(_t("Value")):
        if value.get("Name") == "BackOffMilliseconds":
            try:
                return max(0.0, int((value.text or "0").strip()) / 1000.0)
            except ValueError:
                return None
    return None


def response_code(message: ET.Element) -> Optional[str]:
    """The error code of one response message, or None when it succeeded."""
    if message.get("ResponseClass") == "Error":
        return message.findtext(_m("ResponseCode")) or "Error"
    return None


def ensure_success(message: ET.Element) -> ET.Element:
    code = response_code(message)
    if code:
        raise error_for(code)
    return message


# --------------------------------------------------------------- request parts

def distinguished_folder(name: str, mailbox: Optional[str] = None) -> ET.Element:
    node = ET.Element(_t("DistinguishedFolderId"), {"Id": name})
    if mailbox:
        ET.SubElement(ET.SubElement(node, _t("Mailbox")), _t("EmailAddress")).text = mailbox
    return node


def folder_id(value: str) -> ET.Element:
    return ET.Element(_t("FolderId"), {"Id": value})


def _shape(parent: ET.Element, tag: str, fields: Sequence[str], *, body_type: Optional[str] = None) -> None:
    shape = ET.SubElement(parent, _m(tag))
    ET.SubElement(shape, _t("BaseShape")).text = "IdOnly"
    if body_type:
        ET.SubElement(shape, _t("BodyType")).text = body_type
    extra = ET.SubElement(shape, _t("AdditionalProperties"))
    for field in fields:
        ET.SubElement(extra, _t("FieldURI"), {"FieldURI": field})


_FOLDER_FIELDS = (
    "folder:DisplayName", "folder:ParentFolderId", "folder:FolderClass",
    "folder:TotalCount", "folder:UnreadCount", "folder:ChildFolderCount",
)
_ITEM_FIELDS = (
    "item:Subject", "item:DateTimeReceived", "item:ParentFolderId", "item:Size",
    "item:HasAttachments", "message:From", "message:IsRead",
)
#: Listing extras newer servers serve. Dropped (once, for the life of the
#: client) when a server refuses them, so listing still works without them.
_ITEM_FIELDS_OPTIONAL = ("item:Preview", "item:WebClientReadFormQueryString")
_READ_FIELDS = (
    "item:Subject", "item:Body", "item:DateTimeReceived", "item:DateTimeSent",
    "item:HasAttachments", "item:Attachments", "item:WebClientReadFormQueryString",
    "message:From", "message:ToRecipients", "message:CcRecipients",
)
#: Server answers that mean "I do not know one of the properties you asked for".
UNSUPPORTED_PROPERTY_CODES = frozenset({
    "ErrorInvalidPropertyRequest", "ErrorSchemaValidation", "ErrorInvalidRequest",
})


def get_well_known_folders(mailbox: Optional[str]) -> ET.Element:
    op = ET.Element(_m("GetFolder"))
    _shape(op, "FolderShape", _FOLDER_FIELDS)
    ids = ET.SubElement(op, _m("FolderIds"))
    for name in _WELL_KNOWN:
        ids.append(distinguished_folder(name, mailbox))
    return op


def find_folders(parent: ET.Element, *, offset: int, page_size: int) -> ET.Element:
    op = ET.Element(_m("FindFolder"), {"Traversal": "Deep"})
    _shape(op, "FolderShape", _FOLDER_FIELDS)
    ET.SubElement(op, _m("IndexedPageFolderView"), {
        "MaxEntriesReturned": str(page_size), "Offset": str(offset), "BasePoint": "Beginning",
    })
    ET.SubElement(op, _m("ParentFolderIds")).append(parent)
    return op


def _comparison(parent: ET.Element, tag: str, field: str, value: str) -> None:
    node = ET.SubElement(parent, _t(tag))
    ET.SubElement(node, _t("FieldURI"), {"FieldURI": field})
    ET.SubElement(ET.SubElement(node, _t("FieldURIOrConstant")), _t("Constant"), {"Value": value})


def find_items(
    folder_ids: Sequence[str],
    *,
    offset: int,
    page_size: int,
    unread_only: bool = False,
    received_after: Optional[str] = None,
    received_before: Optional[str] = None,
    received_up_to: Optional[str] = None,
    query: Optional[str] = None,
    rich: bool = True,
) -> ET.Element:
    """One page of messages per folder, newest first.

    ``query`` is a keyword search. A keyword search and a structured filter
    cannot travel in the same request, so with ``query`` set the unread and
    received bounds are ignored here and applied by the caller to the rows.
    """
    op = ET.Element(_m("FindItem"), {"Traversal": "Shallow"})
    _shape(op, "ItemShape", _ITEM_FIELDS + (_ITEM_FIELDS_OPTIONAL if rich else ()))
    ET.SubElement(op, _m("IndexedPageItemView"), {
        "MaxEntriesReturned": str(page_size), "Offset": str(offset), "BasePoint": "Beginning",
    })
    if not query:
        conditions = []
        if unread_only:
            conditions.append(("IsEqualTo", "message:IsRead", "false"))
        if received_after:
            conditions.append(("IsGreaterThanOrEqualTo", "item:DateTimeReceived", received_after))
        if received_before:
            conditions.append(("IsLessThan", "item:DateTimeReceived", received_before))
        if received_up_to:
            conditions.append(("IsLessThanOrEqualTo", "item:DateTimeReceived", received_up_to))
        if conditions:
            restriction = ET.SubElement(op, _m("Restriction"))
            holder = ET.SubElement(restriction, _t("And")) if len(conditions) > 1 else restriction
            for tag, field, value in conditions:
                _comparison(holder, tag, field, value)
    order = ET.SubElement(ET.SubElement(op, _m("SortOrder")), _t("FieldOrder"), {"Order": "Descending"})
    ET.SubElement(order, _t("FieldURI"), {"FieldURI": "item:DateTimeReceived"})
    parents = ET.SubElement(op, _m("ParentFolderIds"))
    for value in folder_ids:
        parents.append(folder_id(value))
    if query:
        ET.SubElement(op, _m("QueryString")).text = query
    return op


def get_item(item_id: str) -> ET.Element:
    op = ET.Element(_m("GetItem"))
    _shape(op, "ItemShape", _READ_FIELDS, body_type="Text")
    ET.SubElement(ET.SubElement(op, _m("ItemIds")), _t("ItemId"), {"Id": item_id})
    return op


# -------------------------------------------------------------- response parts

def _int(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_folder(node: ET.Element) -> Dict[str, Any]:
    identifier = node.find(_t("FolderId"))
    parent = node.find(_t("ParentFolderId"))
    return {
        "id": identifier.get("Id") if identifier is not None else None,
        "parent_id": parent.get("Id") if parent is not None else None,
        "name": node.findtext(_t("DisplayName")) or "",
        "kind": _local(node.tag),
        "folder_class": node.findtext(_t("FolderClass")) or "",
        "total_count": _int(node.findtext(_t("TotalCount"))),
        "unread_count": _int(node.findtext(_t("UnreadCount"))),
    }


def is_mail_folder(folder: Dict[str, Any]) -> bool:
    """Plain mail folders only — not calendars, contacts, tasks or saved searches."""
    if folder.get("kind") != "Folder":
        return False
    folder_class = folder.get("folder_class") or ""
    return not folder_class or folder_class == "IPF.Note" or folder_class.startswith("IPF.Note.")


def parse_folders_page(message: ET.Element) -> Dict[str, Any]:
    root = message.find(_m("RootFolder"))
    if root is None:
        return {"folders": [], "last": True}
    holder = root.find(_t("Folders"))
    return {
        "folders": [parse_folder(node) for node in (holder if holder is not None else [])],
        "last": (root.get("IncludesLastItemInRange") or "true").lower() == "true",
    }


def mailbox_label(node: Optional[ET.Element]) -> str:
    """``Name <address>`` for a recipient, tolerating either part missing.

    Listings return the display name only, and addresses inside the
    organization can come back as a directory path rather than an email
    address — neither is useful to show, so the name wins there.
    """
    if node is None:
        return ""
    box = node if _local(node.tag) == "Mailbox" else node.find(_t("Mailbox"))
    if box is None:
        return ""
    name = (box.findtext(_t("Name")) or "").strip()
    address = (box.findtext(_t("EmailAddress")) or "").strip()
    if "@" not in address:
        return name or address
    if name and name.lower() != address.lower():
        return f"{name} <{address}>"
    return address


def _web_link(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    return value if value.lower().startswith(("https://", "http://")) else None


def parse_item(node: ET.Element) -> Dict[str, Any]:
    identifier = node.find(_t("ItemId"))
    parent = node.find(_t("ParentFolderId"))
    read = node.findtext(_t("IsRead"))
    return {
        "id": identifier.get("Id") if identifier is not None else None,
        "subject": node.findtext(_t("Subject")) or "",
        "received": node.findtext(_t("DateTimeReceived")) or node.findtext(_t("DateTimeSent")),
        "parent_id": parent.get("Id") if parent is not None else None,
        "size": _int(node.findtext(_t("Size"))),
        "from": mailbox_label(node.find(_t("From"))),
        "is_read": None if read is None else read.strip().lower() == "true",
        "preview": node.findtext(_t("Preview")),
        "web_url": _web_link(node.findtext(_t("WebClientReadFormQueryString"))),
    }


def parse_items_page(message: ET.Element) -> Dict[str, Any]:
    root = message.find(_m("RootFolder"))
    if root is None:
        return {"items": [], "last": True}
    holder = root.find(_t("Items"))
    return {
        "items": [parse_item(node) for node in (holder if holder is not None else [])],
        "last": (root.get("IncludesLastItemInRange") or "true").lower() == "true",
    }


def parse_message(message: ET.Element) -> Dict[str, Any]:
    holder = message.find(_m("Items"))
    node = holder[0] if holder is not None and len(holder) else None
    if node is None:
        raise error_for("ErrorItemNotFound")
    body = node.find(_t("Body"))
    attachments = node.find(_t("Attachments"))
    return {
        **parse_item(node),
        "to": [label for label in (mailbox_label(m) for m in node.findall(f"{_t('ToRecipients')}/{_t('Mailbox')}")) if label],
        "cc": [label for label in (mailbox_label(m) for m in node.findall(f"{_t('CcRecipients')}/{_t('Mailbox')}")) if label],
        "body": (body.text or "") if body is not None else "",
        "body_type": (body.get("BodyType") or "Text") if body is not None else "Text",
        "attachments": [
            name for name in (a.findtext(_t("Name")) for a in (attachments if attachments is not None else [])) if name
        ],
    }

"""In-memory Exchange Web Services provider for tests and sandbox runs.

Speaks the read operations of the EWS SOAP protocol against a small mailbox
model, so the mail connector can be exercised end to end at the HTTP boundary
without an Exchange server. It evaluates what the connector asks for (folder
scope, paging, unread / received restrictions, keyword queries) rather than
returning canned answers, so a wrong request produces a wrong result.

``FakeExchange.handle(body)`` is transport-free: unit tests call it from a
patched ``requests.Session.post``; ``tools/agent/mock_exchange_server.py``
serves it over HTTP with Windows authentication.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

S = "http://schemas.xmlsoap.org/soap/envelope/"
T = "http://schemas.microsoft.com/exchange/services/2006/types"
M = "http://schemas.microsoft.com/exchange/services/2006/messages"
E = "http://schemas.microsoft.com/exchange/services/2006/errors"

READ_OPERATIONS = {"GetFolder", "FindFolder", "FindItem", "GetItem"}
_ROLES = {
    "inbox": "Inbox", "sentitems": "Sent Items", "deleteditems": "Deleted Items",
    "drafts": "Drafts", "junkemail": "Junk Email",
}


def _t(name: str) -> str:
    return f"{{{T}}}{name}"


def _m(name: str) -> str:
    return f"{{{M}}}{name}"


@dataclass
class Message:
    subject: str
    sender_name: str
    sender_email: str
    received: str  # ISO-8601 UTC, e.g. 2026-10-05T06:00:00Z
    body: str = ""
    is_read: bool = True
    to: List[Tuple[str, str]] = field(default_factory=list)
    cc: List[Tuple[str, str]] = field(default_factory=list)
    attachments: List[str] = field(default_factory=list)
    html: bool = False
    kind: str = "Message"
    id: str = ""


@dataclass
class Folder:
    id: str
    name: str
    parent_id: Optional[str]
    kind: str = "Folder"            # Folder | CalendarFolder | ContactsFolder | SearchFolder
    folder_class: str = "IPF.Note"
    role: Optional[str] = None      # inbox | sentitems | ...
    messages: List[Message] = field(default_factory=list)


class Mailbox:
    """One mailbox: a folder tree under a root, with the well-known folders."""

    def __init__(self, key: str, *, localized: Optional[Dict[str, str]] = None):
        self.key = key
        self.root_id = self._new_id("root")
        self.folders: Dict[str, Folder] = {}
        for role, name in _ROLES.items():
            self.add_folder((localized or {}).get(role, name), role=role)
        self.add_folder("Calendar", kind="CalendarFolder", folder_class="IPF.Appointment")
        self.add_folder("Contacts", kind="ContactsFolder", folder_class="IPF.Contact")

    def _new_id(self, name: str) -> str:
        raw = f"{self.key}|{name}|{len(getattr(self, 'folders', {}))}".encode()
        return "AAMk" + base64.b64encode(raw).decode()

    def add_folder(self, name: str, *, parent: Optional[str] = None, role: Optional[str] = None,
                   kind: str = "Folder", folder_class: str = "IPF.Note") -> Folder:
        parent_id = self.by_path(parent).id if parent else self.root_id
        folder = Folder(id=self._new_id(name), name=name, parent_id=parent_id, kind=kind,
                        folder_class=folder_class, role=role)
        self.folders[folder.id] = folder
        return folder

    def by_role(self, role: str) -> Folder:
        return next(f for f in self.folders.values() if f.role == role)

    def by_path(self, path: str) -> Folder:
        node_parent = self.root_id
        found = None
        for part in path.split("/"):
            found = next(f for f in self.folders.values() if f.parent_id == node_parent and f.name == part)
            node_parent = found.id
        return found

    def add(self, path: str, message: Message) -> Message:
        folder = self.by_path(path)
        raw = f"{self.key}|{folder.id}|{len(folder.messages)}|{message.subject}".encode()
        message.id = "AAMkAD" + base64.b64encode(raw).decode()
        folder.messages.append(message)
        return message

    def descendants(self, folder_id: str) -> List[Folder]:
        out, frontier = [], [folder_id]
        while frontier:
            current = frontier.pop(0)
            children = [f for f in self.folders.values() if f.parent_id == current]
            out.extend(children)
            frontier.extend(c.id for c in children)
        return out


class FakeExchange:
    """A tiny Exchange organization.

    ``own`` is the mailbox of the authenticated identity; ``shared`` are other
    mailboxes addressed by email. Behaviour knobs mirror real server answers.
    """

    def __init__(self):
        self.own = Mailbox("own")
        self.shared: Dict[str, Mailbox] = {}
        #: Addresses the identity may not open at all.
        self.denied: set = set()
        #: Addresses where only the Inbox subtree is visible (folder-level sharing).
        self.inbox_only: set = set()
        #: Folder ids that became unreadable after they were listed.
        self.hidden_folders: set = set()
        #: Refuse a keyword search that names several folders in one request.
        self.reject_multi_folder_search = False
        #: Refuse the newer listing properties (older servers).
        self.reject_listing_extras = False
        #: Answer the next N requests with "server busy".
        self.busy_requests = 0
        self.busy_back_off_ms = 250
        #: Serve the read link the way newer servers do.
        self.web_link_base: Optional[str] = "https://mail.example.com/owa/"
        #: Server-side cap on a page, as a real server applies.
        self.max_page = 1000
        #: Every operation name received, in order.
        self.operations: List[str] = []
        self.requests: List[ET.Element] = []

    def add_shared(self, address: str, **kwargs) -> Mailbox:
        mailbox = Mailbox(address.lower(), **kwargs)
        self.shared[address.lower()] = mailbox
        return mailbox

    # --------------------------------------------------------------- lookup

    def _mailbox_for(self, node: ET.Element) -> Mailbox:
        address = node.findtext(f"{_t('Mailbox')}/{_t('EmailAddress')}")
        if not address:
            return self.own
        key = address.lower()
        if key not in self.shared:
            raise _Refused("ErrorNonExistentMailbox")
        if key in self.denied:
            # Exchange answers a mailbox the caller has no rights on as "not found".
            raise _Refused("ErrorFolderNotFound")
        return self.shared[key]

    def _all_mailboxes(self) -> List[Mailbox]:
        return [self.own, *self.shared.values()]

    def _folder(self, folder_id: str) -> Tuple[Mailbox, Folder]:
        for mailbox in self._all_mailboxes():
            if folder_id in mailbox.folders:
                if mailbox.key in self.denied or folder_id in self.hidden_folders:
                    raise _Refused("ErrorFolderNotFound")
                return mailbox, mailbox.folders[folder_id]
        raise _Refused("ErrorFolderNotFound")

    def _resolve_parent(self, node: ET.Element) -> Tuple[Mailbox, str]:
        """(mailbox, folder id) a DistinguishedFolderId / FolderId names."""
        if node.tag == _t("FolderId"):
            mailbox, folder = self._folder(node.get("Id"))
            return mailbox, folder.id
        mailbox = self._mailbox_for(node)
        name = node.get("Id")
        if name == "msgfolderroot":
            if mailbox.key in self.inbox_only:
                raise _Refused("ErrorAccessDenied")
            return mailbox, mailbox.root_id
        if name in _ROLES:
            if mailbox.key in self.inbox_only and name != "inbox":
                raise _Refused("ErrorFolderNotFound")
            return mailbox, mailbox.by_role(name).id
        raise _Refused("ErrorFolderNotFound")

    # ------------------------------------------------------------ rendering

    @staticmethod
    def _folder_xml(folder: Folder) -> str:
        unread = sum(1 for m in folder.messages if not m.is_read)
        return (
            f'<t:{folder.kind}><t:FolderId Id="{escape(folder.id)}" ChangeKey="AQAAAA=="/>'
            f'<t:ParentFolderId Id="{escape(folder.parent_id or "")}"/>'
            f"<t:FolderClass>{escape(folder.folder_class)}</t:FolderClass>"
            f"<t:DisplayName>{escape(folder.name)}</t:DisplayName>"
            f"<t:TotalCount>{len(folder.messages)}</t:TotalCount>"
            f"<t:ChildFolderCount>0</t:ChildFolderCount>"
            + (f"<t:UnreadCount>{unread}</t:UnreadCount>" if folder.kind == "Folder" else "")
            + f"</t:{folder.kind}>"
        )

    def _item_xml(self, message: Message, folder: Folder, fields: List[str], *, body_type: str = "Text") -> str:
        parts = [f'<t:ItemId Id="{escape(message.id)}" ChangeKey="CQAAAA=="/>']
        if "item:ParentFolderId" in fields:
            parts.append(f'<t:ParentFolderId Id="{escape(folder.id)}"/>')
        if "item:Subject" in fields:
            parts.append(f"<t:Subject>{escape(message.subject)}</t:Subject>")
        if "item:Body" in fields:
            served = "HTML" if message.html else body_type
            parts.append(f'<t:Body BodyType="{served}">{escape(message.body)}</t:Body>')
        if "item:Attachments" in fields and message.attachments:
            parts.append("<t:Attachments>" + "".join(
                f'<t:FileAttachment><t:AttachmentId Id="att{i}"/><t:Name>{escape(name)}</t:Name></t:FileAttachment>'
                for i, name in enumerate(message.attachments)) + "</t:Attachments>")
        if "item:DateTimeReceived" in fields:
            parts.append(f"<t:DateTimeReceived>{message.received}</t:DateTimeReceived>")
        if "item:Size" in fields:
            parts.append(f"<t:Size>{len(message.body.encode()) + 512}</t:Size>")
        if "item:HasAttachments" in fields:
            parts.append(f"<t:HasAttachments>{str(bool(message.attachments)).lower()}</t:HasAttachments>")
        if "item:Preview" in fields:
            parts.append(f"<t:Preview>{escape(' '.join(message.body.split())[:255])}</t:Preview>")
        if "item:WebClientReadFormQueryString" in fields and self.web_link_base:
            link = f"{self.web_link_base}?ItemID={message.id[:24]}&exvsurl=1&viewmodel=ReadMessageItem"
            parts.append(f"<t:WebClientReadFormQueryString>{escape(link)}</t:WebClientReadFormQueryString>")
        if "message:ToRecipients" in fields and message.to:
            parts.append("<t:ToRecipients>" + "".join(_box(n, a) for n, a in message.to) + "</t:ToRecipients>")
        if "message:CcRecipients" in fields and message.cc:
            parts.append("<t:CcRecipients>" + "".join(_box(n, a) for n, a in message.cc) + "</t:CcRecipients>")
        if "message:From" in fields:
            # Listings carry the display name only; a full read adds the address.
            address = message.sender_email if "item:Body" in fields else None
            parts.append(f"<t:From>{_box(message.sender_name, address)}</t:From>")
        if "message:IsRead" in fields:
            parts.append(f"<t:IsRead>{str(message.is_read).lower()}</t:IsRead>")
        return f"<t:{message.kind}>{''.join(parts)}</t:{message.kind}>"

    # ------------------------------------------------------------ operations

    def handle(self, body: bytes) -> Tuple[int, bytes]:
        """Answer one SOAP request: ``(http status, response body)``."""
        envelope = ET.fromstring(body)
        operation = envelope.find(f"{{{S}}}Body")[0]
        name = operation.tag.rsplit("}", 1)[-1]
        self.operations.append(name)
        self.requests.append(operation)
        if self.busy_requests > 0:
            self.busy_requests -= 1
            return 500, _fault("ErrorServerBusy", back_off_ms=self.busy_back_off_ms)
        if name not in READ_OPERATIONS:
            return 500, _fault("ErrorInvalidRequest")
        version = envelope.find(f"{{{S}}}Header/{_t('RequestServerVersion')}")
        if version is None or not version.get("Version"):
            return 500, _fault("ErrorInvalidServerVersion")
        messages = getattr(self, f"_{name}")(operation)
        return 200, _envelope(name, messages)

    def _fields(self, operation: ET.Element, shape: str) -> List[str]:
        return [f.get("FieldURI") for f in operation.findall(f"{_m(shape)}/{_t('AdditionalProperties')}/{_t('FieldURI')}")]

    def _GetFolder(self, operation: ET.Element) -> List[str]:
        out = []
        for node in operation.find(_m("FolderIds")):
            try:
                mailbox, folder_id = self._resolve_parent(node)
                out.append(_ok("GetFolder", f"<m:Folders>{self._folder_xml(mailbox.folders[folder_id])}</m:Folders>"))
            except _Refused as refusal:
                out.append(_error("GetFolder", refusal.code))
        return out

    def _FindFolder(self, operation: ET.Element) -> List[str]:
        if operation.get("Traversal") != "Deep":
            return [_error("FindFolder", "ErrorInvalidOperation")]
        view = operation.find(_m("IndexedPageFolderView"))
        offset, size = int(view.get("Offset")), min(int(view.get("MaxEntriesReturned")), self.max_page)
        out = []
        for node in operation.find(_m("ParentFolderIds")):
            try:
                mailbox, folder_id = self._resolve_parent(node)
            except _Refused as refusal:
                out.append(_error("FindFolder", refusal.code))
                continue
            everything = mailbox.descendants(folder_id)
            page = everything[offset:offset + size]
            last = offset + len(page) >= len(everything)
            out.append(_ok("FindFolder", (
                f'<m:RootFolder IndexedPagingOffset="{offset + len(page)}" TotalItemsInView="{len(everything)}" '
                f'IncludesLastItemInRange="{str(last).lower()}"><t:Folders>'
                + "".join(self._folder_xml(f) for f in page) + "</t:Folders></m:RootFolder>")))
        return out

    def _FindItem(self, operation: ET.Element) -> List[str]:
        fields = self._fields(operation, "ItemShape")
        if self.reject_listing_extras and {"item:Preview", "item:WebClientReadFormQueryString"} & set(fields):
            return [_error("FindItem", "ErrorInvalidPropertyRequest")]
        view = operation.find(_m("IndexedPageItemView"))
        offset, size = int(view.get("Offset")), min(int(view.get("MaxEntriesReturned")), self.max_page)
        query = operation.findtext(_m("QueryString"))
        restriction = operation.find(_m("Restriction"))
        if query is not None and restriction is not None:
            return [_error("FindItem", "ErrorInvalidOperation")]
        parents = list(operation.find(_m("ParentFolderIds")))
        if query is not None and len(parents) > 1 and self.reject_multi_folder_search:
            return [_error("FindItem", "ErrorInvalidOperation") for _ in parents]
        descending = operation.find(f"{_m('SortOrder')}/{_t('FieldOrder')}")
        out = []
        for node in parents:
            try:
                _, folder_id = self._resolve_parent(node)
                _, folder = self._folder(folder_id)
            except _Refused as refusal:
                out.append(_error("FindItem", refusal.code))
                continue
            rows = [m for m in folder.messages
                    if _matches_restriction(m, restriction) and _matches_query(m, query)]
            if descending is not None:
                rows.sort(key=lambda m: m.received, reverse=descending.get("Order") == "Descending")
            page = rows[offset:offset + size]
            last = offset + len(page) >= len(rows)
            out.append(_ok("FindItem", (
                f'<m:RootFolder IndexedPagingOffset="{offset + len(page)}" TotalItemsInView="{len(rows)}" '
                f'IncludesLastItemInRange="{str(last).lower()}"><t:Items>'
                + "".join(self._item_xml(m, folder, fields) for m in page) + "</t:Items></m:RootFolder>")))
        return out

    def _GetItem(self, operation: ET.Element) -> List[str]:
        fields = self._fields(operation, "ItemShape")
        body_type = operation.findtext(f"{_m('ItemShape')}/{_t('BodyType')}") or "Best"
        out = []
        for node in operation.find(_m("ItemIds")):
            wanted = node.get("Id")
            hit = None
            for mailbox in self._all_mailboxes():
                if mailbox.key in self.denied:
                    continue
                for folder in mailbox.folders.values():
                    for message in folder.messages:
                        if message.id == wanted and folder.id not in self.hidden_folders:
                            hit = (message, folder)
            if hit is None:
                well_formed = bool(re.fullmatch(r"[A-Za-z0-9+/=_-]{8,}", wanted or ""))
                out.append(_error("GetItem", "ErrorItemNotFound" if well_formed else "ErrorInvalidIdMalformed"))
                continue
            out.append(_ok("GetItem", f"<m:Items>{self._item_xml(hit[0], hit[1], fields, body_type=body_type)}</m:Items>"))
        return out


class _Refused(Exception):
    def __init__(self, code: str):
        self.code = code


def _box(name: Optional[str], address: Optional[str]) -> str:
    inner = f"<t:Name>{escape(name)}</t:Name>" if name else ""
    if address:
        inner += f"<t:EmailAddress>{escape(address)}</t:EmailAddress><t:RoutingType>SMTP</t:RoutingType>"
    return f"<t:Mailbox>{inner}</t:Mailbox>"


def _ok(operation: str, inner: str) -> str:
    return (f'<m:{operation}ResponseMessage ResponseClass="Success">'
            f"<m:ResponseCode>NoError</m:ResponseCode>{inner}</m:{operation}ResponseMessage>")


def _error(operation: str, code: str) -> str:
    return (f'<m:{operation}ResponseMessage ResponseClass="Error">'
            f"<m:MessageText>The request failed.</m:MessageText><m:ResponseCode>{code}</m:ResponseCode>"
            f"<m:DescriptiveLinkKey>0</m:DescriptiveLinkKey></m:{operation}ResponseMessage>")


def _envelope(operation: str, messages: List[str]) -> bytes:
    return (
        f'<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="{S}"><s:Header>'
        f'<h:ServerVersionInfo xmlns:h="{T}" MajorVersion="15" MinorVersion="2" Version="V2017_07_11"/></s:Header>'
        f'<s:Body><m:{operation}Response xmlns:m="{M}" xmlns:t="{T}"><m:ResponseMessages>'
        + "".join(messages) + f"</m:ResponseMessages></m:{operation}Response></s:Body></s:Envelope>"
    ).encode()


def _fault(code: str, *, back_off_ms: Optional[int] = None) -> bytes:
    extra = ""
    if back_off_ms is not None:
        extra = (f'<t:MessageXml xmlns:t="{T}"><t:Value Name="BackOffMilliseconds">{back_off_ms}</t:Value>'
                 "</t:MessageXml>")
    return (
        f'<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="{S}"><s:Body><s:Fault>'
        "<faultcode>s:Server</faultcode><faultstring>The request failed.</faultstring><detail>"
        f'<e:ResponseCode xmlns:e="{E}">{code}</e:ResponseCode>{extra}</detail></s:Fault></s:Body></s:Envelope>'
    ).encode()


def _matches_restriction(message: Message, restriction: Optional[ET.Element]) -> bool:
    if restriction is None:
        return True
    holder = restriction[0]
    conditions = list(holder) if holder.tag == _t("And") else [holder]
    for condition in conditions:
        field_uri = condition.find(_t("FieldURI")).get("FieldURI")
        value = condition.find(f"{_t('FieldURIOrConstant')}/{_t('Constant')}").get("Value")
        actual = {"message:IsRead": str(message.is_read).lower(), "item:DateTimeReceived": message.received}[field_uri]
        operator = condition.tag.rsplit("}", 1)[-1]
        if operator == "IsEqualTo" and actual != value:
            return False
        if operator == "IsGreaterThanOrEqualTo" and not actual >= value:
            return False
        if operator == "IsLessThan" and not actual < value:
            return False
        if operator not in ("IsEqualTo", "IsGreaterThanOrEqualTo", "IsLessThan"):
            raise AssertionError(f"unexpected restriction {operator}")
    return True


_TERM = re.compile(r'(?:(from|subject):)?(?:"([^"]*)"|(\S+))', re.IGNORECASE)


def _matches_query(message: Message, query: Optional[str]) -> bool:
    """A small keyword search: terms are ANDed unless joined by OR;
    ``from:`` and ``subject:`` scope a term; quotes make a phrase."""
    if query is None:
        return True

    def term_hits(scope: Optional[str], text: str) -> bool:
        text = text.lower()
        sender = f"{message.sender_name} {message.sender_email}".lower()
        if scope and scope.lower() == "from":
            return text in sender
        if scope and scope.lower() == "subject":
            return text in message.subject.lower()
        return text in f"{message.subject} {message.body} {sender}".lower()

    alternatives = re.split(r"\s+OR\s+", query.strip())
    for alternative in alternatives:
        terms = [(m.group(1), m.group(2) if m.group(2) is not None else m.group(3))
                 for m in _TERM.finditer(alternative)]
        if terms and all(term_hits(scope, text) for scope, text in terms):
            return True
    return False

"""Read-only mail connector for on-premises Exchange Server.

Reads mailboxes through Exchange Web Services with Windows authentication
(NTLM or Kerberos) — no cloud tenant or app registration involved. The client
advertises only the mail capabilities, so the agent is offered ``list_emails``
/ ``search_email`` / ``read_email`` and nothing else.

Every request runs as the configured identity and Exchange enforces its
permissions. The connector only decides which mailboxes it asks for: the
identity's own mailbox, plus any shared mailboxes named in the connection.
Nothing is indexed or copied; mail is always read live.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from app.data_sources.clients import _exchange_ews as ews
from app.data_sources.clients._exchange_ews import ACCESS_CODES, EwsTransport, ExchangeError
from app.data_sources.clients._windows_auth_session import WindowsAuthSessions
from app.data_sources.clients.base import Capability, DataSourceClient
from app.data_sources.clients.mail_common import (
    clamp_mail_results,
    decode_cursor,
    encode_cursor,
    parse_mail_datetime,
    strip_html,
)
from app.data_sources.clients.progress import discovery_progress

DEFAULT_EWS_PATH = "/EWS/Exchange.asmx"
MAX_SHARED_MAILBOXES = 20

# One round-trip per page keeps each call fast and the cursor fine-grained.
_PAGE_SIZE = 100
_FOLDER_PAGE_SIZE = 200
# Bound on the folder tree across all mailboxes of one connection.
_MAX_FOLDERS = 500
# Safety valve on the requests one list/search call may send.
_MAX_REQUESTS = 30
# Folders searched per request when a search spans a whole mailbox.
_SEARCH_BATCH = 20

_ADDRESS_RE = re.compile(r"^[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[^@\s,;<>\"]+$")
_MAILBOX_ID_PREFIX = "mailbox:"
# Accepted spellings for the well-known folders, whatever the server's language.
_ALIASES = {
    "inbox": "inbox",
    "sentitems": "sentitems", "sent": "sentitems", "sent items": "sentitems",
    "deleteditems": "deleteditems", "deleted": "deleteditems", "deleted items": "deleteditems", "trash": "deleteditems",
    "drafts": "drafts",
    "junkemail": "junkemail", "junk": "junkemail", "junk email": "junkemail", "spam": "junkemail",
}
# A mailbox-wide search leaves these out, as a mail client's search does.
_NOT_SEARCHED = ("deleteditems", "junkemail")
# A mailbox-wide listing answers "what came in": it also leaves out what the
# owner wrote. Name any of these folders to list it.
_NOT_LISTED = ("deleteditems", "junkemail", "sentitems", "drafts")


def parse_shared_mailboxes(value: Any) -> List[str]:
    """Normalise the shared-mailbox setting to a de-duplicated address list."""
    if value is None:
        return []
    parts = value if isinstance(value, (list, tuple)) else re.split(r"[,;\s]+", str(value))
    out: List[str] = []
    for part in parts:
        address = str(part or "").strip()
        if not address:
            continue
        if not _ADDRESS_RE.match(address):
            raise ValueError(f"Shared mailbox {address!r} is not an email address.")
        if address.lower() not in {a.lower() for a in out}:
            out.append(address)
    if len(out) > MAX_SHARED_MAILBOXES:
        raise ValueError(f"At most {MAX_SHARED_MAILBOXES} shared mailboxes can be listed on one connection.")
    return out


def normalize_ews_url(value: str, allow_http: bool = False) -> str:
    """Validate the server URL and fill in the standard service path."""
    parsed = urlsplit(str(value or "").strip())
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Server URL must be an HTTP(S) URL without credentials, query, or fragment.")
    if parsed.scheme == "http" and not allow_http:
        raise ValueError("HTTPS is required. Enable Allow HTTP only for an isolated test lab.")
    path = parsed.path.rstrip("/") or DEFAULT_EWS_PATH
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def planner_scope_note(config: Optional[dict], per_user: bool) -> str:
    """Whose mail a connection reads, in words the planner can repeat.

    A service-account connection and a per-user one look identical from the
    tool surface, and the model otherwise calls a shared team mailbox "your
    inbox" — which is wrong and, for a person asking about their own mail,
    misleading.
    """
    config = config or {}
    try:
        shared = parse_shared_mailboxes(config.get("shared_mailboxes"))
    except ValueError:
        shared = []
    own = config.get("include_own_mailbox", True) not in (False, "false", "False", 0)
    if per_user:
        parts = ["Each user reads with their own Windows sign-in, live; nothing is indexed."]
        if own:
            parts.append("The default mailbox is the asking user's own mailbox.")
        if shared:
            parts.append("Shared mailboxes on this connection: " + ", ".join(shared)
                         + " — a user only sees the ones their account may open.")
    else:
        parts = [
            "Everyone reads through one shared service account, live; nothing is indexed. "
            "This is NOT the asking user's personal mailbox — name a mailbox by its address, "
            "never as \"your inbox\" or \"your mail\"."
        ]
        if shared:
            default = "" if own else f" The default mailbox is {shared[0]}."
            parts.append("Team mailboxes on this connection: " + ", ".join(shared) + "." + default)
        if own:
            parts.append("The service account's own mailbox is the default.")
    parts.append("An empty count here does NOT mean no mail.")
    return " ".join(parts)


class ExchangeOnpremClient(DataSourceClient):
    """Exchange Server mailboxes, shaped for the mail agent tools."""

    capabilities = {Capability.LIST_EMAILS, Capability.READ_EMAIL, Capability.SEARCH_EMAILS}

    def __init__(
        self,
        ews_url: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        kerberos: bool = False,
        principal: Optional[str] = None,
        shared_mailboxes: Any = None,
        include_own_mailbox: bool = True,
        allow_http: bool = False,
        **kwargs,
    ):
        self.ews_url = normalize_ews_url(ews_url, bool(allow_http))
        self.shared_mailboxes = parse_shared_mailboxes(shared_mailboxes)
        self.include_own_mailbox = bool(include_own_mailbox)
        if not self.include_own_mailbox and not self.shared_mailboxes:
            raise ValueError("List at least one shared mailbox, or include the account's own mailbox.")
        # None is the authenticated identity's own mailbox.
        self._mailboxes: List[Optional[str]] = ([None] if self.include_own_mailbox else []) + list(self.shared_mailboxes)
        self._sessions = WindowsAuthSessions(username, password, kerberos=bool(kerberos), principal=principal)
        self._ews = EwsTransport(self.ews_url, self._sessions, username=username, password=password)
        self._folders: Optional[List[dict]] = None
        self._unreachable: Dict[str, str] = {}
        self._rich_listing = True
        self._batch_search = True

    @property
    def description(self) -> str:
        return "Exchange Server mailbox (read-only)"

    @property
    def is_document_based(self) -> bool:
        return True

    # ------------------------------------------------------------- folders

    def _label(self, mailbox: Optional[str]) -> str:
        """Path prefix of a mailbox. The default mailbox has none."""
        return "" if mailbox == self._mailboxes[0] else str(mailbox)

    def _mailbox_folders(self, mailbox: Optional[str]) -> List[dict]:
        """Mail folders of one mailbox, with paths, counts and well-known roles."""
        well_known: Dict[str, dict] = {}
        for name, message in zip(ews._WELL_KNOWN, self._ews.call(ews.get_well_known_folders(mailbox))):
            code = ews.response_code(message)
            if code is None:
                holder = message.find(ews._m("Folders"))
                if holder is not None and len(holder):
                    well_known[name] = ews.parse_folder(holder[0])
            elif name == "inbox":
                # No Inbox means the identity cannot open this mailbox at all.
                raise ews.error_for(code)
            elif code not in ACCESS_CODES:
                raise ews.error_for(code)
        role_by_id = {f["id"]: name for name, f in well_known.items() if f.get("id")}
        inbox = well_known.get("inbox")
        if inbox is None:
            raise ExchangeError("BadResponse", "Exchange returned an incomplete answer. Try again.")

        # Walk from the top of the mailbox. With folder-level sharing only the
        # Inbox may be visible; then walk from there instead.
        try:
            found = self._walk(ews.distinguished_folder("msgfolderroot", mailbox))
        except ExchangeError as exc:
            if exc.code not in ACCESS_CODES:
                raise
            found = [inbox] + self._walk(ews.folder_id(inbox["id"]))

        by_id = {f["id"]: f for f in found if f.get("id")}
        label = self._label(mailbox)

        def path_of(folder: dict) -> str:
            parts, seen, node = [], set(), folder
            while node is not None and node["id"] not in seen:
                seen.add(node["id"])
                parts.append(node["name"])
                node = by_id.get(node.get("parent_id"))
            return "/".join(([label] if label else []) + parts[::-1])

        rows = [{
            "id": f["id"],
            "name": f["name"],
            "path": path_of(f),
            "unread_count": f.get("unread_count"),
            "total_count": f.get("total_count"),
            "mailbox": label,
            "well_known": role_by_id.get(f["id"]),
        } for f in found if f.get("id") and ews.is_mail_folder(f)]
        rows.sort(key=lambda r: (r["well_known"] != "inbox", r["path"].lower()))
        return rows

    def _walk(self, parent) -> List[dict]:
        out: List[dict] = []
        offset = 0
        while len(out) < _MAX_FOLDERS:
            message = self._ews.call(ews.find_folders(parent, offset=offset, page_size=_FOLDER_PAGE_SIZE))[0]
            page = ews.parse_folders_page(ews.ensure_success(message))
            out.extend(page["folders"])
            offset += len(page["folders"])
            if page["last"] or not page["folders"]:
                break
        return out

    def list_mail_folders(self) -> List[dict]:
        """Every mail folder of every mailbox on this connection.

        A shared mailbox the identity cannot open is skipped, not fatal: with
        per-user sign-in each person holds different rights. If no mailbox can
        be opened at all, the first refusal is raised.
        """
        if self._folders is not None:
            return self._folders
        rows: List[dict] = []
        first_error: Optional[ExchangeError] = None
        self._unreachable = {}
        for mailbox in self._mailboxes:
            try:
                rows.extend(self._mailbox_folders(mailbox))
            except ExchangeError as exc:
                if exc.code not in ACCESS_CODES:
                    raise
                self._unreachable[mailbox or "own mailbox"] = str(exc)
                first_error = first_error or exc
        if not rows and first_error is not None:
            raise first_error
        self._folders = rows[:_MAX_FOLDERS]
        return self._folders

    def unreachable_note(self) -> Optional[str]:
        """Names the configured mailboxes this identity could not open.

        Skipping them silently let the agent assume it had read them — it told
        a user it could see a shared mailbox their account has no rights on.
        """
        self.list_mail_folders()
        if not self._unreachable:
            return None
        return ("NOT reachable with this account (no permission, or no such mailbox): "
                + ", ".join(sorted(self._unreachable)) + ". Nothing from there is included.")

    def _mailbox_scope(self, label: str, address: str) -> dict:
        """A pseudo-folder meaning "this whole mailbox" (search only)."""
        return {"id": _MAILBOX_ID_PREFIX + address, "name": address, "path": address,
                "mailbox": label, "whole_mailbox": True}

    def _inbox(self, label: str = "") -> dict:
        for row in self.list_mail_folders():
            if row["mailbox"] == label and row["well_known"] == "inbox":
                return row
        # The default mailbox was unreachable: fall back to the first Inbox there is.
        for row in self.list_mail_folders():
            if row["well_known"] == "inbox":
                return row
        raise ValueError("No Inbox is available on this connection.")

    def resolve_mail_folder(self, folder: str) -> dict:
        """Map a folder id, path, unique display name, well-known name or a
        shared mailbox address to its row. Matching is case-insensitive; an
        unknown or ambiguous name raises with the paths that do exist."""
        wanted = str(folder or "").strip().strip("/")
        folders = self.list_mail_folders()
        for row in folders:
            if row["id"] == wanted:
                return row
        low = wanted.lower()
        if low.startswith(_MAILBOX_ID_PREFIX):
            low = low[len(_MAILBOX_ID_PREFIX):]
        for address in self.shared_mailboxes:
            if address.lower() == low:
                label = self._label(address)
                if any(row["mailbox"] == label for row in folders):
                    return self._mailbox_scope(label, address)
                raise ValueError(
                    f"The mailbox {address} cannot be opened with this account: it has no permission "
                    "on it, or the mailbox does not exist. Do not retry; tell the user."
                )
        by_path = [row for row in folders if row["path"].lower() == low]
        if len(by_path) == 1:
            return by_path[0]
        by_name = [row for row in folders if row["name"].lower() == low]
        if len(by_name) == 1:
            return by_name[0]
        role = _ALIASES.get(low)
        if role:
            default_label = self._label(self._mailboxes[0])
            for row in folders:
                if row["well_known"] == role and row["mailbox"] == default_label:
                    return row
        choices = by_name or folders
        listing = ", ".join(row["path"] for row in choices[:60])
        reason = "matches several folders" if by_name else "was not found"
        raise ValueError(f"Mail folder {folder!r} {reason}. Folders on this connection: {listing}")

    # ------------------------------------------------------------ messages

    def _item(self, raw: dict, folder_paths: Dict[str, str]) -> dict:
        preview = " ".join(str(raw.get("preview") or "").split())
        subject = raw.get("subject") or "(no subject)"
        return {
            "id": raw.get("id"),
            "name": subject,
            "path": subject,
            "mime_type": "message/rfc822",
            "size": raw.get("size"),
            "from": raw.get("from") or "",
            "modified_at": raw.get("received"),
            "web_url": raw.get("web_url"),
            "preview": preview or None,
            "is_read": raw.get("is_read"),
            "folder": folder_paths.get(raw.get("parent_id") or ""),
        }

    def _find(self, folder_ids: List[str], *, offset: int, page_size: int, **filters) -> List[dict]:
        """One page per folder: ``[{items, last, error}]`` in ``folder_ids`` order."""
        def send(rich: bool):
            return self._ews.call(ews.find_items(folder_ids, offset=offset, page_size=page_size, rich=rich, **filters))

        try:
            messages = send(self._rich_listing)
            unsupported = self._rich_listing and any(
                ews.response_code(m) in ews.UNSUPPORTED_PROPERTY_CODES for m in messages
            )
        except ExchangeError as exc:
            if not self._rich_listing or exc.code not in ews.UNSUPPORTED_PROPERTY_CODES:
                raise
            unsupported = True
        if unsupported:
            # An older server does not serve the listing extras (preview, web
            # link). Ask without them from now on rather than fail the listing.
            self._rich_listing = False
            messages = send(False)
        pages = []
        for message in messages:
            code = ews.response_code(message)
            pages.append({"items": [], "last": True, "error": code} if code else
                         {**ews.parse_items_page(message), "error": None})
        if len(pages) != len(folder_ids):
            raise ExchangeError("BadResponse", "Exchange returned an incomplete answer. Try again.")
        return pages

    def _folder_paths(self) -> Dict[str, str]:
        return {row["id"]: row["path"] for row in (self._folders or [])}

    def list_messages(
        self,
        folder: Optional[str] = None,
        unread_only: bool = False,
        received_after: Optional[str] = None,
        received_before: Optional[str] = None,
        max_results: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Messages newest first, filtered by the server.

        Returns ``{items, next_cursor, folder, note}``. With a ``folder`` the
        listing is that folder. With no folder — or a shared mailbox address —
        it is the whole mailbox: every folder mail arrives in, so a message a
        rule filed into a subfolder is not missed. Sent, draft, deleted and
        junk mail is left out of a whole-mailbox listing; name those folders
        to list them. ``next_cursor`` is None only when every matching message
        has been returned.
        """
        limit = clamp_mail_results(max_results)
        after, before = parse_mail_datetime(received_after), parse_mail_datetime(received_before)
        filters = {
            "unread_only": bool(unread_only),
            "received_after": _ews_time(after),
            "received_before": _ews_time(before),
        }
        row = self.resolve_mail_folder(folder) if folder else None
        if row is None or row.get("whole_mailbox"):
            label = row["mailbox"] if row else self._label(self._mailboxes[0])
            return self._list_mailbox(label, row, limit, filters, cursor)

        state = decode_cursor(cursor)
        if state and state.get("f") != row["id"]:
            raise ValueError("Invalid cursor — pass back the next_cursor value exactly as returned, with the same folder.")
        offset = max(0, int(state.get("o") or 0))
        paths = self._folder_paths()
        items: List[dict] = []
        next_cursor = None
        for _ in range(_MAX_REQUESTS):
            page = self._find([row["id"]], offset=offset, page_size=min(_PAGE_SIZE, limit - len(items)), **filters)[0]
            if page["error"]:
                raise ews.error_for(page["error"])
            items.extend(self._item(raw, paths) for raw in page["items"])
            offset += len(page["items"])
            if page["last"] or not page["items"]:
                break
            if len(items) >= limit:
                next_cursor = encode_cursor({"f": row["id"], "o": offset})
                break
        else:
            next_cursor = encode_cursor({"f": row["id"], "o": offset})
        return {"items": items, "next_cursor": next_cursor, "folder": row, "note": None}

    def _list_mailbox(self, label: str, scope: Optional[dict], limit: int, filters: dict,
                      cursor: Optional[str]) -> Dict[str, Any]:
        """One newest-first page across the incoming folders of a mailbox.

        Folders cannot be paged together by offset, so the page boundary is a
        point in time: the cursor is the received time of the last message
        returned (plus the ids already returned at that exact second), and the
        next call asks every folder for what is at or before it. Each call is
        one request per batch of folders, with no per-folder state to carry.
        """
        folders = [f for f in self.list_mail_folders() if f["mailbox"] == label]
        skipped = [f["path"] for f in folders if f["well_known"] in _NOT_LISTED]
        targets = [
            f for f in folders
            if f["total_count"] != 0
            and not (filters["unread_only"] and f["unread_count"] == 0)
            and f["path"] not in skipped
            and not any(f["path"].startswith(prefix + "/") for prefix in skipped)
        ]
        note = "Covers every folder of the mailbox except Sent, Drafts, Deleted and Junk; name one of those to list it."
        others = sorted({f["mailbox"] for f in self.list_mail_folders() if f["mailbox"] and f["mailbox"] != label})
        if scope is None and others:
            note += " Other mailboxes on this connection (pass the address as the folder): " + ", ".join(others) + "."
        if self._unreachable:
            note += " " + self.unreachable_note()
        per_request = _SEARCH_BATCH if self._batch_search else 1
        if len(targets) > _MAX_REQUESTS * per_request:
            # Too many folders to read together in one call: stay correct on a
            # smaller scope and say so, rather than return a partial mix.
            targets = [self._inbox(label)]
            note = ("This mailbox has too many folders to list together, so only its Inbox is covered. "
                    "Name a folder to list another one.")

        state = decode_cursor(cursor)
        if state and (state.get("m") != label or not state.get("t")):
            raise ValueError("Invalid cursor — pass back the next_cursor value exactly as returned, with the same folder.")
        watermark = state.get("t")
        already = set(state.get("s") or [])
        paths = self._folder_paths()
        rows: List[dict] = []
        # A folder that had more than one page to give only vouches for the
        # messages down to its oldest returned one. Nothing older than the
        # newest such point may be emitted yet, or that folder's next message
        # would be skipped.
        horizon = ""
        position = 0
        while position < len(targets):
            size = _SEARCH_BATCH if self._batch_search else 1
            batch = targets[position:position + size]
            pages = self._find([t["id"] for t in batch], offset=0, page_size=limit + len(already),
                               received_up_to=watermark, **filters)
            if len(batch) > 1 and any(p["error"] and p["error"] not in ACCESS_CODES for p in pages):
                self._batch_search = False  # this server reads one folder per request
                continue
            for page in pages:
                if page["error"]:
                    if page["error"] in ACCESS_CODES:
                        continue  # folder vanished or is not visible to this identity
                    raise ews.error_for(page["error"])
                rows.extend(page["items"])
                if not page["last"] and page["items"]:
                    horizon = max(horizon, min(r.get("received") or "" for r in page["items"]))
            position += len(batch)
        rows = [r for r in rows if _short_id(r.get("id")) not in already]
        rows.sort(key=lambda r: r.get("received") or "", reverse=True)
        ready = [r for r in rows if (r.get("received") or "") >= horizon]
        page_rows = ready[:limit]
        next_cursor = None
        if (len(rows) > len(page_rows) or horizon) and page_rows and page_rows[-1].get("received"):
            last = page_rows[-1]["received"]
            same_second = {_short_id(r["id"]) for r in page_rows if r.get("received") == last}
            if last == watermark:
                same_second |= already
            next_cursor = encode_cursor({"m": label, "t": last, "s": sorted(same_second)})
        return {
            "items": [self._item(raw, paths) for raw in page_rows],
            "next_cursor": next_cursor,
            "folder": scope,
            "note": note,
        }

    def search_messages(
        self,
        query: str,
        folder: Optional[str] = None,
        unread_only: bool = False,
        received_after: Optional[str] = None,
        received_before: Optional[str] = None,
        max_results: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Keyword search, newest first.

        With a ``folder`` the search stays in that folder. A shared mailbox
        address searches that whole mailbox. With neither, every mailbox on the
        connection is searched. Deleted and junk mail is left out of a
        mailbox-wide search; name those folders to search them.

        Exchange has no single "search the mailbox" request, so a wide search
        visits the mailbox's folders in turn. Empty folders are skipped.
        """
        query = str(query or "").strip()
        if not query:
            raise ValueError("Enter something to search for.")
        row = self.resolve_mail_folder(folder) if folder else None
        targets = self._search_targets(row)
        limit = clamp_mail_results(max_results)
        after, before = parse_mail_datetime(received_after), parse_mail_datetime(received_before)

        def keep(raw: dict) -> bool:
            if unread_only and raw.get("is_read") is not False:
                return False
            if after or before:
                try:
                    received = parse_mail_datetime(raw.get("received"))
                except ValueError:
                    received = None
                if received is None or (after and received < after) or (before and received >= before):
                    return False
            return True

        state = decode_cursor(cursor)
        start, offset = 0, 0
        if state:
            ids = [t["id"] for t in targets]
            if state.get("f") not in ids:
                raise ValueError("Invalid cursor — pass back the next_cursor value exactly as returned, with the same folder.")
            start, offset = ids.index(state["f"]), max(0, int(state.get("o") or 0))

        paths = self._folder_paths()
        items: List[dict] = []
        budget = _MAX_REQUESTS

        def stop_at(target: dict, at: int) -> Dict[str, Any]:
            return self._search_result(items, encode_cursor({"f": target["id"], "o": at}), row)

        def consume(target: dict, page: dict, base: int) -> Optional[int]:
            """Take rows from one page; returns the offset to resume at when full."""
            for index, raw in enumerate(page["items"]):
                if not keep(raw):
                    continue
                if len(items) >= limit:
                    return base + index
                items.append(self._item(raw, paths))
            return None

        position = start
        while position < len(targets):
            # A folder being resumed mid-way is read alone; the rest go in batches.
            size = 1 if (offset or not self._batch_search) else _SEARCH_BATCH
            batch = targets[position:position + size]
            if budget <= 0:
                return stop_at(batch[0], offset)
            budget -= 1
            try:
                pages = self._find([t["id"] for t in batch], offset=offset, page_size=_PAGE_SIZE, query=query)
            except ExchangeError as exc:
                if len(batch) == 1 or exc.code in ("Unauthorized", "Unreachable", "TlsError", "ErrorServerBusy"):
                    raise
                # This server will not search several folders in one request.
                self._batch_search = False
                continue
            if len(batch) > 1 and any(p["error"] and p["error"] not in ACCESS_CODES for p in pages):
                # Same refusal, reported per folder instead of for the request.
                self._batch_search = False
                continue
            for target, page in zip(batch, pages):
                if page["error"]:
                    if page["error"] in ACCESS_CODES:
                        continue  # folder vanished or is not visible to this identity
                    raise ews.error_for(page["error"])
                at = offset
                while True:
                    full_at = consume(target, page, at)
                    if full_at is not None:
                        return stop_at(target, full_at)
                    at += len(page["items"])
                    if page["last"] or not page["items"]:
                        break
                    if budget <= 0:
                        return stop_at(target, at)
                    budget -= 1
                    page = self._find([target["id"]], offset=at, page_size=_PAGE_SIZE, query=query)[0]
                    if page["error"]:
                        if page["error"] in ACCESS_CODES:
                            break
                        raise ews.error_for(page["error"])
                offset = 0
            position += len(batch)
            offset = 0
        return self._search_result(items, None, row)

    def _search_targets(self, row: Optional[dict]) -> List[dict]:
        if row is not None and not row.get("whole_mailbox"):
            return [row]
        folders = self.list_mail_folders()
        if row is not None:
            folders = [f for f in folders if f["mailbox"] == row["mailbox"]]
        skipped = [f["path"] for f in folders if f["well_known"] in _NOT_SEARCHED]
        return [
            f for f in folders
            if f["total_count"] != 0
            and f["path"] not in skipped
            and not any(f["path"].startswith(prefix + "/") for prefix in skipped)
        ]

    def _search_result(self, items: List[dict], next_cursor: Optional[str], row: Optional[dict]) -> Dict[str, Any]:
        # A wide search gathers folder by folder; present what was gathered newest first.
        items.sort(key=lambda item: item.get("modified_at") or "", reverse=True)
        # Only a search across every mailbox can be silently narrower than asked.
        note = self.unreachable_note() if row is None else None
        return {"items": items, "next_cursor": next_cursor, "folder": row, "note": note}

    # ---------------------------------------------------------------- reads

    def read_raw_bytes(self):
        raise AttributeError("Mail messages have no original bytes to preview")

    def list_files(self, folder_id: Optional[str] = None, recursive: Optional[bool] = None, **_) -> List[dict]:
        return self.list_messages(folder=folder_id)["items"]

    def search_files(self, query: str, max_results: Optional[int] = None, **_) -> List[dict]:
        return self.search_messages(query, max_results=max_results)["items"]

    def read_file(self, file_id: str, **_) -> Any:
        message = self._ews.call(ews.get_item(str(file_id)))[0]
        m = ews.parse_message(ews.ensure_success(message))
        body = m["body"]
        if m["body_type"].lower() == "html":
            body = strip_html(body)
        header = (
            f"Subject: {m['subject'] or '(no subject)'}\n"
            f"From: {m['from']}\nTo: {', '.join(m['to'])}\n"
        )
        if m["cc"]:
            header += f"Cc: {', '.join(m['cc'])}\n"
        header += f"Date: {m['received'] or ''}\n"
        if m["attachments"]:
            header += f"Attachments: {', '.join(m['attachments'])}\n"
        # Omitted when the server serves none, so the model never cites an
        # empty link as if it were a real one.
        if m["web_url"]:
            header += f"Link: {m['web_url']}\n"
        return header + "\n" + body

    # ------------------------------------------------- connection lifecycle

    def test_connection(self) -> dict:
        try:
            folders = self.list_mail_folders()
        except Exception as exc:
            return {"success": False, "message": str(exc)}
        inboxes = [row for row in folders if row["well_known"] == "inbox"]
        parts = []
        default_name = self._mailboxes[0] or "Inbox"
        for row in inboxes:
            where = row["mailbox"] or default_name
            count = row.get("total_count")
            parts.append(f"{where}: {count} message(s)" if count is not None else where)
        message = "Connected to Exchange. " + "; ".join(parts) + "."
        if self._unreachable:
            message += (
                " Not reachable with this account: "
                + ", ".join(sorted(self._unreachable))
                + " — check the address and the account's permissions."
            )
        return {"success": True, "message": message}

    # Mailboxes are always read live; they never create catalog rows.
    @discovery_progress
    def get_schemas(self, *args, progress_callback=None, **kwargs) -> List:
        return []

    def get_schema(self, table_name: str):
        return None

    def prompt_schema(self) -> str:
        return "Exchange Server mail is searched and read live with the connection's Windows identity."

    def execute_query(self, query: Optional[str] = None, table_name: Optional[str] = None, **kwargs):
        message_id = kwargs.get("message_id") or kwargs.get("file_id") or table_name
        if message_id:
            return self.read_file(str(message_id))
        if query:
            return self.search_files(query)
        return self.list_files()


def _short_id(value: Optional[str]) -> str:
    """A compact stand-in for a message id inside a paging cursor."""
    return hashlib.sha256(str(value or "").encode()).hexdigest()[:10]


def _ews_time(moment) -> Optional[str]:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None

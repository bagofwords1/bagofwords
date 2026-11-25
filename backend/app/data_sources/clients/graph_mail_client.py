"""Microsoft Graph (Outlook) mail client with a mail-named agent surface.

Reuses GraphDriveClient's Entra OAuth (delegated per-user + service-principal
fallback) and HTTP plumbing. Message payloads reuse the file transport while
the client advertises distinct mail capabilities:

  list_emails  -> messages in the mailbox or one folder, newest first, filtered
                  server-side by unread / received window, paged to completion
  search_email -> Graph $search (KQL) over the mailbox or one folder, paged
  read_email   -> the message rendered as plain text (headers + link + body)

The shared execution layer still materializes message bodies as session files
when needed, but the planner and the user see email vocabulary throughout.
"""
from __future__ import annotations
from app.data_sources.clients.progress import discovery_progress

import urllib.parse
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import httpx

from app.data_sources.clients.base import Capability
from app.data_sources.clients.connector_actions import (
    EFFECT_EXTERNAL,
    EFFECT_MODIFY,
    ActionError,
    ConnectorAction,
    ConnectorActionsMixin,
)
from app.data_sources.clients.graph_drive_client import GRAPH_BASE, GraphDriveClient, graph_write
from app.data_sources.clients.mail_common import (
    clamp_mail_results,
    decode_cursor,
    encode_cursor,
    graph_search_value,
    parse_mail_datetime,
    strip_html,
)

_MESSAGE_SELECT = "id,subject,from,receivedDateTime,webLink,isRead,bodyPreview,parentFolderId"
_FOLDER_SELECT = "id,displayName,parentFolderId,childFolderCount,unreadItemCount,totalItemCount"
# Graph serves up to 1000 messages per page, but large pages risk a gateway
# timeout (504); 100 keeps each round-trip fast and the cursor fine-grained.
_PAGE_SIZE = 100
# Safety valve on one call's page walk (500 results / 100 per page = 5 pages
# normally; filtered searches may skip many rows).
_MAX_PAGES = 30
# Folder-tree walk bound — mailboxes with thousands of folders exist.
_MAX_FOLDERS = 500


def _graph_dt(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _received(m: dict) -> Optional[datetime]:
    try:
        return parse_mail_datetime(m.get("receivedDateTime"))
    except ValueError:
        return None


# ---------------------------------------------------------------- actions
#
# Write actions (BOW-209). Each one is a ConnectionTool row on an Outlook
# connection, off until a connection admin turns it on, and every call passes
# the tool policy (allow / ask / auto / deny) of the agent it goes through.
# Graph references: message-update, message-move, user-post-messages,
# message-createreply / createreplyall / createforward, message-send, user-sendmail.

_MSG_ID = {"type": "string", "description": "The message id, exactly as list_emails / search_email returned it."}
_RECIPIENTS = {"type": "array", "items": {"type": "string"}, "description": "Email addresses."}


def _recipients(addrs) -> list:
    if isinstance(addrs, str):
        addrs = [a for a in addrs.replace(";", ",").split(",")]
    return [{"emailAddress": {"address": a.strip()}} for a in (addrs or []) if str(a).strip()]


def _short(text, n=80) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _body(text: str) -> dict:
    content = str(text or "")
    is_html = "<" in content and ">" in content and any(t in content.lower() for t in ("<p", "<br", "<div", "<b>", "<ul", "<a "))
    return {"contentType": "html" if is_html else "text", "content": content}


def _draft_preview(a) -> str:
    mode = a.get("mode") or "new"
    text = _short(a.get("subject") or a.get("body"), 60)
    if mode in ("reply", "reply_all"):
        label = "Reply-all draft" if mode == "reply_all" else "Reply draft"
        return f"{label}: {text}"
    if mode == "forward":
        return f"Forward draft to {_join(a.get('to')) or '—'}: {text}"
    return f"Draft to {_join(a.get('to')) or '—'}: {text}"


def _join(addrs) -> str:
    if isinstance(addrs, str):
        return addrs
    return ", ".join(str(a) for a in (addrs or []))


OUTLOOK_ACTIONS = (
    ConnectorAction(
        name="flag_email",
        description=("Flag, unflag or complete an email, and/or mark it read or unread. "
                     "Changes only the signed-in user's own mailbox."),
        effect=EFFECT_MODIFY,
        input_schema={
            "type": "object",
            "properties": {
                "message_id": _MSG_ID,
                "flag": {"type": "string", "enum": ["flagged", "complete", "notFlagged"],
                         "description": "New follow-up flag state."},
                "is_read": {"type": "boolean", "description": "Mark as read (true) or unread (false)."},
            },
            "required": ["message_id"],
        },
        required_scopes=(("Mail.ReadWrite",),),
        handler="action_flag_email",
        preview=lambda a: " · ".join(x for x in (
            f"Flag: {a.get('flag')}" if a.get("flag") else "",
            ("Mark read" if a.get("is_read") else "Mark unread") if a.get("is_read") is not None else "",
        ) if x) or "Update email",
    ),
    ConnectorAction(
        name="move_email",
        description=("Move an email to another folder of the signed-in user's mailbox (a folder path or "
                     "name such as 'Inbox/Invoices', or archive, inbox, deleteditems). "
                     "Returns the message's new id: Graph gives a moved message a new id."),
        effect=EFFECT_MODIFY,
        input_schema={
            "type": "object",
            "properties": {
                "message_id": _MSG_ID,
                "destination": {"type": "string", "description": "Folder path, name or id, or archive / inbox / deleteditems."},
            },
            "required": ["message_id", "destination"],
        },
        required_scopes=(("Mail.ReadWrite",),),
        handler="action_move_email",
        preview=lambda a: f"Move to {a.get('destination')}",
    ),
    ConnectorAction(
        name="create_draft",
        description=("Save an email as a draft in the user's Drafts folder — nothing is sent. "
                     "mode=new writes a new message; reply / reply_all / forward start from "
                     "reply_to_message_id. Send it later with send_email(draft_id=...)."),
        effect=EFFECT_MODIFY,
        input_schema={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["new", "reply", "reply_all", "forward"], "default": "new"},
                "to": _RECIPIENTS,
                "cc": _RECIPIENTS,
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Plain text, or simple HTML."},
                "reply_to_message_id": {"type": "string", "description": "Required for reply / reply_all / forward."},
            },
            "required": ["body"],
        },
        required_scopes=(("Mail.ReadWrite",),),
        handler="action_create_draft",
        preview=lambda a: _draft_preview(a),
    ),
    ConnectorAction(
        name="send_email",
        description=("Send an email from the signed-in user's mailbox. Pass draft_id to send a draft "
                     "created with create_draft (preferred), or to / subject / body to send a new "
                     "message directly. Recipients receive it immediately; it cannot be recalled."),
        effect=EFFECT_EXTERNAL,
        input_schema={
            "type": "object",
            "properties": {
                "draft_id": {"type": "string", "description": "Id of a draft from create_draft."},
                "to": _RECIPIENTS,
                "cc": _RECIPIENTS,
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
        },
        required_scopes=(("Mail.Send",),),
        handler="action_send_email",
        preview=lambda a: (f"Send draft {a.get('draft_id')}" if a.get("draft_id") else
                           f"Send to {_join(a.get('to')) or '—'}: {_short(a.get('subject'), 60)}"),
    ),
)



class GraphMailClient(ConnectorActionsMixin, GraphDriveClient):
    """Outlook/Exchange mail over Microsoft Graph, shaped as a file source.

    Declares the MAIL capabilities (not the file ones) so the agent surfaces the
    mail-named tools — ``list_emails`` / ``read_email`` / ``search_email`` —
    instead of ``list_files`` / ``read_file`` / ``search_files``. The underlying
    methods keep their file-tool names (``list_files``/``read_file``/
    ``search_files`` below) since older callers delegate straight to them; the
    mail tools call ``list_messages`` / ``search_messages`` for filters, folders
    and paging.
    """

    capabilities = {Capability.LIST_EMAILS, Capability.READ_EMAIL, Capability.SEARCH_EMAILS}
    actions = OUTLOOK_ACTIONS

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("mode", "outlook_mail")
        super().__init__(*args, **kwargs)
        self._mail_folders: Optional[List[dict]] = None

    @staticmethod
    def _addr(obj: dict) -> str:
        return (((obj or {}).get("emailAddress") or {}).get("address")) or ""

    def _msg_to_item(self, m: dict) -> dict:
        preview = " ".join(str(m.get("bodyPreview") or "").split())
        folder = self._folder_path_by_id().get(m.get("parentFolderId") or "")
        return {
            "id": m.get("id"),
            "name": m.get("subject") or "(no subject)",
            "path": m.get("subject") or "(no subject)",
            "mime_type": "message/rfc822",
            "from": self._addr(m.get("from")),
            "modified_at": m.get("receivedDateTime"),
            # Graph's own deep link to the message in Outlook on the web. Carried
            # through FileEntry.web_url by list_files/search_files, so the agent
            # can cite a message the user can actually click through to — the
            # opaque Graph id is useless outside a tool call. GmailMailClient
            # already does this; this keeps the two mailboxes at parity.
            "web_url": m.get("webLink"),
            # First ~255 chars of the body, as plain text (Graph bodyPreview).
            # Lets the agent triage a whole window from the listing instead of
            # summarising from subject lines or opening every message.
            "preview": preview or None,
            "is_read": m.get("isRead"),
            "folder": folder,
        }

    # ------------------------------------------------------------- folders

    def list_mail_folders(self) -> List[dict]:
        """Every mail folder (nested ones included) with its path and counts.

        ``/me/mailFolders`` returns top-level folders only; custom folders such
        as ``Inbox/Telex In`` live under ``childFolders``, so the tree is walked.
        """
        cached = getattr(self, "_mail_folders", None)
        if cached is not None:
            return cached
        if not getattr(self, "_user_token_provided", True):
            return []
        out: List[dict] = []
        seen: set = set()

        def _walk(url: Optional[str], parent_path: str) -> None:
            while url and len(out) < _MAX_FOLDERS:
                data = self._get(url)
                for f in data.get("value") or []:
                    if f.get("id") in seen:
                        continue
                    seen.add(f.get("id"))
                    name = f.get("displayName") or ""
                    path = f"{parent_path}/{name}" if parent_path else name
                    out.append({
                        "id": f.get("id"),
                        "name": name,
                        "path": path,
                        "unread_count": f.get("unreadItemCount"),
                        "total_count": f.get("totalItemCount"),
                    })
                    if f.get("childFolderCount"):
                        fid = urllib.parse.quote(str(f.get("id")), safe="")
                        _walk(
                            f"/me/mailFolders/{fid}/childFolders?$top=100&$select={_FOLDER_SELECT}",
                            path,
                        )
                url = data.get("@odata.nextLink")

        _walk(f"/me/mailFolders?$top=100&$select={_FOLDER_SELECT}", "")
        self._mail_folders = out
        return out

    def _folder_path_by_id(self) -> Dict[str, str]:
        # Only label rows when the tree was already fetched for this call —
        # never trigger a folder walk just to decorate a listing.
        return {f["id"]: f["path"] for f in (getattr(self, "_mail_folders", None) or []) if f.get("id")}

    def resolve_mail_folder(self, folder: str) -> dict:
        """Map a folder id, path (``Inbox/Telex In``) or unique display name to
        the folder row. Matching is case-insensitive; an unknown or ambiguous
        name raises with the folder paths that do exist, so the agent can retry
        with a real one instead of guessing a KQL ``folder:`` term."""
        wanted = str(folder or "").strip().strip("/")
        folders = self.list_mail_folders()
        for f in folders:
            if f["id"] == wanted:
                return f
        low = wanted.lower()
        by_path = [f for f in folders if f["path"].lower() == low]
        if len(by_path) == 1:
            return by_path[0]
        by_name = [f for f in folders if f["name"].lower() == low]
        if len(by_name) == 1:
            return by_name[0]
        # Well-known names (inbox, sentitems, archive, …) are valid ids on Graph.
        if low in {"inbox", "sentitems", "deleteditems", "drafts", "junkemail", "archive"}:
            data = self._get(f"/me/mailFolders/{low}?$select={_FOLDER_SELECT}")
            return {
                "id": data.get("id"), "name": data.get("displayName") or wanted,
                "path": data.get("displayName") or wanted,
                "unread_count": data.get("unreadItemCount"), "total_count": data.get("totalItemCount"),
            }
        choices = by_name or folders
        listing = ", ".join(f["path"] for f in choices[:60])
        reason = "matches several folders" if by_name else "was not found"
        raise ValueError(f"Mail folder {folder!r} {reason}. Folders in this mailbox: {listing}")

    # --------------------------------------------------------------- paging

    def _collect(
        self,
        first_url: str,
        max_results: int,
        cursor: Optional[str],
        keep: Callable[[dict], bool] = lambda m: True,
    ) -> Dict[str, Any]:
        """Walk Graph pages until ``max_results`` rows are kept.

        The cursor carries the URL of the page being read plus an offset into
        it, so a resumed call continues exactly where the previous one stopped
        even when that was mid-page. Only Graph URLs are ever followed: the
        cursor comes back through the model, and following an arbitrary URL
        would hand the user's bearer token to whatever host it named.
        """
        state = decode_cursor(cursor)
        url = state.get("u") or first_url
        offset = int(state.get("o") or 0)
        if not str(url).startswith(GRAPH_BASE + "/"):
            raise ValueError("Invalid cursor — pass back the next_cursor value exactly as returned.")
        items: List[dict] = []
        for _ in range(_MAX_PAGES):
            data = self._get(url)
            page = data.get("value") or []
            nxt = data.get("@odata.nextLink")
            for i in range(offset, len(page)):
                m = page[i]
                if not keep(m):
                    continue
                if len(items) >= max_results:
                    return {"items": items, "next_cursor": encode_cursor({"u": url, "o": i})}
                items.append(self._msg_to_item(m))
            if not nxt:
                return {"items": items, "next_cursor": None}
            url, offset = nxt, 0
            if len(items) >= max_results:
                return {"items": items, "next_cursor": encode_cursor({"u": url, "o": 0})}
        return {"items": items, "next_cursor": encode_cursor({"u": url, "o": offset})}

    def _messages_base(self, folder: Optional[str]) -> tuple:
        if not folder:
            return "/me/messages", None
        row = self.resolve_mail_folder(folder)
        fid = urllib.parse.quote(str(row["id"]), safe="")
        return f"/me/mailFolders/{fid}/messages", row

    def list_messages(
        self,
        folder: Optional[str] = None,
        unread_only: bool = False,
        received_after: Optional[str] = None,
        received_before: Optional[str] = None,
        max_results: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Messages newest first, filtered server-side, paged to completion.

        Returns ``{items, next_cursor, folder}``. ``next_cursor`` is None only
        when every matching message has been returned.
        """
        # Mailbox enumeration goes through /me/*, which only works with a
        # delegated user token. Without one (e.g. the admin's credential test, or
        # admin-save indexing before any user has signed in), return an empty
        # inventory rather than 400. The real enumeration runs per-user once a
        # user completes OAuth. Mirrors the OneDrive guard in the parent client.
        if not getattr(self, "_user_token_provided", True):
            return {"items": [], "next_cursor": None, "folder": None}
        n = clamp_mail_results(max_results)
        after = parse_mail_datetime(received_after)
        before = parse_mail_datetime(received_before)
        base, folder_row = self._messages_base(folder)
        if folder_row is None and getattr(self, "_mail_folders", None) is None:
            # Across the whole mailbox a message can sit in Inbox, an archive
            # folder and Deleted Items — label each row with its folder so the
            # agent can tell copies apart. One cheap walk, cached per client.
            try:
                self.list_mail_folders()
            except Exception:
                self._mail_folders = []
        # Graph rejects `$orderby` on a property that is not ALSO the first
        # term of `$filter` (InefficientFilter), so receivedDateTime always
        # leads; an open lower bound keeps that true for unread-only listings.
        clauses = []
        if after or unread_only or before:
            clauses.append(f"receivedDateTime ge {_graph_dt(after) if after else '1900-01-01T00:00:00Z'}")
        if before:
            clauses.append(f"receivedDateTime lt {_graph_dt(before)}")
        if unread_only:
            clauses.append("isRead eq false")
        params = {
            "$top": str(min(n, _PAGE_SIZE)),
            "$select": _MESSAGE_SELECT,
            "$orderby": "receivedDateTime desc",
        }
        if clauses:
            params["$filter"] = " and ".join(clauses)
        first = str(httpx.URL(f"{GRAPH_BASE}{base}", params=params))
        page = self._collect(first, n, cursor)
        page["folder"] = folder_row
        return page

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
        """KQL search, paged. Graph refuses ``$filter``/``$orderby`` alongside
        ``$search``, so unread / date bounds are applied to the returned rows.
        """
        if not getattr(self, "_user_token_provided", True):
            return {"items": [], "next_cursor": None, "folder": None}
        if not (query or "").strip():
            raise ValueError("search_email needs a non-empty query; use list_emails to browse.")
        n = clamp_mail_results(max_results)
        after = parse_mail_datetime(received_after)
        before = parse_mail_datetime(received_before)
        base, folder_row = self._messages_base(folder)
        params = {"$search": graph_search_value(query), "$top": str(_PAGE_SIZE), "$select": _MESSAGE_SELECT}
        first = str(httpx.URL(f"{GRAPH_BASE}{base}", params=params))

        def keep(m: dict) -> bool:
            if unread_only and m.get("isRead"):
                return False
            got = _received(m)
            if after and got and got < after:
                return False
            if before and got and got >= before:
                return False
            return True

        # No early stop at the lower bound: search results are ordered by SENT
        # time, so a message sent earlier but received inside the window can
        # follow older ones. Graph caps a search at 1,000 results, so filtering
        # the whole result set stays bounded.
        page = self._collect(first, n, cursor, keep=keep)
        page["folder"] = folder_row
        return page

    # ----------------------------------------------- file-shaped compatibility

    # Same as Teams: a message has no original bytes, and the drive reader
    # inherited from GraphDriveClient would fail on a message id — hide it so
    # the browse route does not offer a preview (hasattr sees AttributeError).
    @property
    def read_raw_bytes(self):
        raise AttributeError("Mail messages have no original bytes to preview")

    def list_files(self, folder_id: Optional[str] = None, recursive: Optional[bool] = None) -> List[dict]:
        return self.list_messages(folder=folder_id)["items"]

    def search_files(self, query: str, max_results: Optional[int] = None, **_) -> List[dict]:
        return self.search_messages(query, max_results=max_results)["items"]

    def read_file(self, file_id: str, **_) -> Any:
        m = self._get(
            f"/me/messages/{file_id}"
            "?$select=subject,from,toRecipients,receivedDateTime,body,bodyPreview,webLink"
        )
        frm = self._addr(m.get("from"))
        to = ", ".join(self._addr(r) for r in (m.get("toRecipients") or []))
        body = m.get("body") or {}
        content = body.get("content") or m.get("bodyPreview") or ""
        if (body.get("contentType") or "").lower() == "html":
            content = strip_html(content)
        header = (
            f"Subject: {m.get('subject') or '(no subject)'}\n"
            f"From: {frm}\nTo: {to}\nDate: {m.get('receivedDateTime') or ''}\n"
        )
        # The link rides in the header block rather than a structured output
        # field because read_file returns rendered TEXT — that text is what
        # reaches the model, what the observation excerpts, and what the
        # cross-turn digest snapshots. A sibling field on ReadFileOutput would
        # need a new client-to-tool channel and would still be invisible in all
        # three. Omitted entirely when Graph doesn't serve one, so the model
        # never sees an empty `Link:` and cites it as a dead URL.
        link = m.get("webLink")
        if link:
            header += f"Link: {link}\n"
        return header + "\n" + content

    # Email has no pre-indexed admin catalog — it's searched/read live per user.
    @discovery_progress
    def get_schemas(self, *args, progress_callback=None, **kwargs) -> List:
        return []

    def test_connection(self) -> dict:
        # Admin-only (service-principal credentials, no user token yet): every
        # mail endpoint here is `/me/*`, which Graph serves for delegated tokens
        # only. Probing it with an app-only token always fails with
        # `/me request is only valid with delegated authentication flow`, and the
        # raw Graph body was surfaced to the admin as if their credentials were
        # wrong. Verify the credentials can mint a token and stop there —
        # GraphDriveClient already does exactly this for OneDrive.
        if not getattr(self, "_user_token_provided", True):
            try:
                self._token()
            except Exception as e:
                return {"success": False, "message": str(e)}
            return {
                "success": True,
                "message": (
                    "Service principal credentials verified. Have a user sign "
                    "in with Microsoft to access their mailbox."
                ),
            }

        try:
            me = self._get("/me?$select=userPrincipalName,displayName")
            who = me.get("userPrincipalName") or me.get("displayName") or "Microsoft account"
        except Exception as e:
            return {"success": False, "message": str(e)}

        # `/me` only proves the token maps to a directory user — it says nothing
        # about the MAILBOX. A user without an Exchange license has a perfectly
        # valid identity but no mailbox, so an identity-only check reported a
        # green "Connected as …" and every mail tool then failed at runtime with
        # `MailboxNotEnabledForRESTAPI`. Probe the mailbox itself so the failure
        # surfaces at connect time, where it is actionable.
        try:
            self._get("/me/messages?$top=1&$select=id")
        except Exception as e:
            detail = str(e)
            if "MailboxNotEnabledForRESTAPI" in detail or "mailbox is either inactive" in detail.lower():
                return {
                    "success": False,
                    "message": (
                        f"Signed in as {who}, but this account has no Exchange mailbox "
                        "(it is inactive, soft-deleted, or missing a Microsoft 365 "
                        "mail license). Assign a mailbox to use this connection."
                    ),
                }
            return {"success": False, "message": f"Signed in as {who}, but the mailbox is unreadable: {detail}"}

        return {"success": True, "message": f"Connected as {who}"}

    # ---------------------------------------------------------- actions
    # Write actions (see OUTLOOK_ACTIONS above). The tool policy has already
    # allowed the call when one of these runs; each sends exactly one write.

    @staticmethod
    def _mid(message_id: str) -> str:
        if not message_id:
            raise ActionError("message_id is required.")
        return urllib.parse.quote(str(message_id), safe="")

    def action_flag_email(self, message_id: str, flag: Optional[str] = None, is_read: Optional[bool] = None) -> dict:
        patch: Dict[str, Any] = {}
        if flag:
            if flag not in ("flagged", "complete", "notFlagged"):
                raise ActionError("flag must be flagged, complete or notFlagged.")
            patch["flag"] = {"flagStatus": flag}
        if is_read is not None:
            patch["isRead"] = bool(is_read)
        if not patch:
            raise ActionError("Nothing to change: pass flag and/or is_read.")
        m = graph_write(self, "PATCH", f"/me/messages/{self._mid(message_id)}", patch)
        return {"message_id": m.get("id") or message_id, "subject": m.get("subject"),
                "flag": ((m.get("flag") or {}).get("flagStatus")), "is_read": m.get("isRead")}

    def action_move_email(self, message_id: str, destination: str) -> dict:
        if not destination:
            raise ActionError("destination is required.")
        try:
            folder = self.resolve_mail_folder(destination)
        except ValueError as e:
            raise ActionError(str(e))
        m = graph_write(self, "POST", f"/me/messages/{self._mid(message_id)}/move", {"destinationId": folder["id"]})
        return {"old_message_id": message_id, "new_message_id": m.get("id"),
                "folder": folder.get("path") or destination, "subject": m.get("subject"),
                "note": "Use new_message_id from now on: Graph gives a moved message a new id."}

    def action_create_draft(self, body: str, mode: str = "new", to=None, cc=None,
                            subject: Optional[str] = None, reply_to_message_id: Optional[str] = None) -> dict:
        mode = (mode or "new").lower()
        if mode == "new":
            msg: Dict[str, Any] = {"subject": subject or "", "body": _body(body),
                                   "toRecipients": _recipients(to)}
            if cc:
                msg["ccRecipients"] = _recipients(cc)
            d = graph_write(self, "POST", "/me/messages", msg)
        elif mode in ("reply", "reply_all", "forward"):
            if not reply_to_message_id:
                raise ActionError(f"reply_to_message_id is required for mode={mode}.")
            verb = {"reply": "createReply", "reply_all": "createReplyAll", "forward": "createForward"}[mode]
            payload: Dict[str, Any] = {"message": {"body": _body(body)}}
            if to:
                payload["message"]["toRecipients"] = _recipients(to)
            if cc:
                payload["message"]["ccRecipients"] = _recipients(cc)
            d = graph_write(self, "POST", f"/me/messages/{self._mid(reply_to_message_id)}/{verb}", payload)
            if subject and d.get("id"):
                graph_write(self, "PATCH", f"/me/messages/{self._mid(d['id'])}", {"subject": subject})
        else:
            raise ActionError("mode must be new, reply, reply_all or forward.")
        return {"draft_id": d.get("id"), "subject": d.get("subject") or subject,
                "to": [self._addr(r) for r in (d.get("toRecipients") or [])], "web_url": d.get("webLink"),
                "sent": False}

    def action_send_email(self, draft_id: Optional[str] = None, to=None, cc=None,
                          subject: Optional[str] = None, body: Optional[str] = None) -> dict:
        if draft_id:
            graph_write(self, "POST", f"/me/messages/{self._mid(draft_id)}/send")
            return {"sent": True, "draft_id": draft_id}
        if not to:
            raise ActionError("Pass draft_id, or to + subject + body.")
        msg: Dict[str, Any] = {"subject": subject or "", "body": _body(body or ""), "toRecipients": _recipients(to)}
        if cc:
            msg["ccRecipients"] = _recipients(cc)
        graph_write(self, "POST", "/me/sendMail", {"message": msg, "saveToSentItems": True})
        return {"sent": True, "to": [r["emailAddress"]["address"] for r in msg["toRecipients"]],
                "subject": subject}

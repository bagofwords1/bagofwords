"""Native Gmail REST API client, exposed through the mail tool vocabulary.

Each Gmail message is normalized to the same file-shaped payload used by the
existing mail tools, but the client advertises only the mail capabilities so
the planner receives ``list_emails`` / ``read_email`` / ``search_email``.
OAuth is delegated per user; no service account or domain-wide delegation is
used in v1.
"""

from __future__ import annotations
from app.data_sources.clients.progress import discovery_progress

import base64
import binascii
import html
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from app.data_sources.clients.base import Capability, DataSourceClient
from app.data_sources.clients.mail_common import (
    clamp_mail_results,
    decode_cursor,
    encode_cursor,
    parse_mail_datetime,
    strip_html,
)

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"
_METADATA_HEADERS = ("Subject", "From", "To", "Date")
# Gmail serves up to 500 ids per page; 100 keeps the per-id metadata fan-out
# of one page bounded and the cursor fine-grained.
_PAGE_SIZE = 100
_MAX_PAGES = 30
_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([^\s;\"']+)", re.IGNORECASE)


class GmailMailClient(DataSourceClient):
    """Read-only Gmail mailbox client using the stable Gmail v1 REST API."""

    capabilities = {
        Capability.LIST_EMAILS,
        Capability.READ_EMAIL,
        Capability.SEARCH_EMAILS,
    }

    def __init__(
        self,
        access_token: Optional[str] = None,
        workspace_domain: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,
        **_ignored,
    ):
        super().__init__()
        self.access_token = access_token
        self.workspace_domain = workspace_domain
        # Test-only injection point for deterministic external-boundary tests.
        self._transport = transport
        self._labels: Optional[List[dict]] = None

    @property
    def description(self) -> str:
        return "Gmail mailbox (signed-in Google account)"

    @property
    def is_document_based(self) -> bool:
        return True

    def _headers(self) -> Dict[str, str]:
        if not self.access_token:
            raise ValueError("Gmail client has no access token. Sign in with Google before using this connection.")
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }

    def _client(self) -> httpx.Client:
        return httpx.Client(
            headers=self._headers(),
            timeout=30,
            follow_redirects=True,
            transport=self._transport,
        )

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
            message = ((payload.get("error") or {}).get("message")) if isinstance(payload, dict) else None
        except Exception:
            message = None
        return message or response.text[:300] or response.reason_phrase

    def _get(
        self,
        path: str,
        *,
        params: Optional[Any] = None,
        client: Optional[httpx.Client] = None,
    ) -> dict:
        url = path if path.startswith("https://") else f"{GMAIL_BASE}{path}"
        if client is not None:
            response = client.get(url, params=params)
        else:
            with self._client() as owned_client:
                response = owned_client.get(url, params=params)
        if response.status_code >= 400:
            raise ValueError(f"Gmail API {response.status_code}: {self._error_message(response)}")
        return response.json()

    @staticmethod
    def _header_map(payload: dict) -> Dict[str, str]:
        values: Dict[str, str] = {}
        for item in payload.get("headers") or []:
            name = str(item.get("name") or "").strip().lower()
            if name and name not in values:
                values[name] = str(item.get("value") or "")
        return values

    @staticmethod
    def _internal_date(value: Any) -> Optional[str]:
        try:
            moment = datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
            return moment.isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _web_url(message: dict) -> str:
        target = message.get("threadId") or message.get("id") or ""
        return f"https://mail.google.com/mail/u/0/#all/{target}"

    def _message_to_item(self, message: dict) -> dict:
        headers = self._header_map(message.get("payload") or {})
        subject = headers.get("subject") or "(no subject)"
        return {
            "id": message.get("id"),
            "name": subject,
            "path": subject,
            "mime_type": "message/rfc822",
            "size": message.get("sizeEstimate"),
            "modified_at": self._internal_date(message.get("internalDate")) or headers.get("date"),
            "web_url": self._web_url(message),
            "from": headers.get("from", ""),
            "thread_id": message.get("threadId"),
            "snippet": message.get("snippet") or "",
            "preview": " ".join(html.unescape(message.get("snippet") or "").split()) or None,
            "is_read": ("UNREAD" not in message["labelIds"]) if isinstance(message.get("labelIds"), list) else None,
        }

    @staticmethod
    def _metadata_params() -> List[tuple[str, str]]:
        params: List[tuple[str, str]] = [("format", "metadata")]
        params.extend(("metadataHeaders", name) for name in _METADATA_HEADERS)
        return params

    # --------------------------------------------------------------- labels

    def list_mail_folders(self) -> List[dict]:
        """Gmail labels, which play the role of folders (INBOX, SENT, user labels)."""
        if self._labels is not None:
            return self._labels
        if not self.access_token:
            return []
        data = self._get("/users/me/labels")
        self._labels = [
            {"id": lab.get("id"), "name": lab.get("name") or lab.get("id"), "path": lab.get("name") or lab.get("id")}
            for lab in data.get("labels") or []
        ]
        return self._labels

    def resolve_mail_folder(self, folder: str) -> dict:
        wanted = str(folder or "").strip().strip("/")
        labels = self.list_mail_folders()
        low = wanted.lower()
        for lab in labels:
            if lab["id"] == wanted or str(lab["name"]).lower() == low:
                row = dict(lab)
                try:
                    detail = self._get(f"/users/me/labels/{lab['id']}")
                    row["unread_count"] = detail.get("messagesUnread")
                    row["total_count"] = detail.get("messagesTotal")
                except Exception:
                    pass
                return row
        listing = ", ".join(str(lab["name"]) for lab in labels[:60])
        raise ValueError(f"Mail folder/label {folder!r} was not found. Labels in this mailbox: {listing}")

    # --------------------------------------------------------------- paging

    def _page_messages(self, params: Dict[str, Any], max_results: int, cursor: Optional[str]) -> Dict[str, Any]:
        """Walk ``messages.list`` pages, fetching metadata for each kept id.

        The cursor is self-contained (query params + page token + offset into
        that page), so a resumed call needs nothing but the cursor.
        """
        state = decode_cursor(cursor)
        if state:
            params = dict(state.get("p") or {})
        token = state.get("t")
        offset = int(state.get("o") or 0)
        rows: List[dict] = []
        # Reuse one HTTP/2-capable client for the list and metadata calls. Gmail
        # messages.list returns only id/threadId, so metadata is a second step.
        with self._client() as client:
            for _ in range(_MAX_PAGES):
                page_params = dict(params)
                if token:
                    page_params["pageToken"] = token
                page = self._get("/users/me/messages", params=page_params, client=client)
                refs = page.get("messages") or []
                nxt = page.get("nextPageToken")
                for i in range(offset, len(refs)):
                    if len(rows) >= max_results:
                        return {"items": rows, "next_cursor": encode_cursor({"p": params, "t": token, "o": i})}
                    message_id = refs[i].get("id")
                    if not message_id:
                        continue
                    message = self._get(
                        f"/users/me/messages/{message_id}",
                        params=self._metadata_params(),
                        client=client,
                    )
                    # Defensive merge: a mocked or partial metadata response may
                    # omit threadId even though the list row had it.
                    if not message.get("threadId") and refs[i].get("threadId"):
                        message["threadId"] = refs[i]["threadId"]
                    rows.append(self._message_to_item(message))
                if not nxt:
                    return {"items": rows, "next_cursor": None}
                token, offset = nxt, 0
                if len(rows) >= max_results:
                    return {"items": rows, "next_cursor": encode_cursor({"p": params, "t": token, "o": 0})}
        return {"items": rows, "next_cursor": encode_cursor({"p": params, "t": token, "o": offset})}

    def _query_params(
        self,
        query: Optional[str],
        folder: Optional[str],
        unread_only: bool,
        received_after: Optional[str],
        received_before: Optional[str],
        max_results: int,
    ) -> tuple:
        terms = [query.strip()] if query and query.strip() else []
        if unread_only:
            terms.append("is:unread")
        after = parse_mail_datetime(received_after)
        before = parse_mail_datetime(received_before)
        # Gmail's after:/before: take epoch seconds for sub-day precision.
        if after:
            terms.append(f"after:{int(after.timestamp())}")
        if before:
            terms.append(f"before:{int(before.timestamp())}")
        params: Dict[str, Any] = {
            "maxResults": min(max_results, _PAGE_SIZE),
            "includeSpamTrash": "false",
            "fields": "messages(id,threadId),nextPageToken,resultSizeEstimate",
        }
        if terms:
            params["q"] = " ".join(terms)
        folder_row = None
        if folder:
            folder_row = self.resolve_mail_folder(folder)
            params["labelIds"] = folder_row["id"]
        return params, folder_row

    def list_messages(
        self,
        folder: Optional[str] = None,
        unread_only: bool = False,
        received_after: Optional[str] = None,
        received_before: Optional[str] = None,
        max_results: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        # Admin save only stores the OAuth app. The mailbox becomes enumerable
        # after an individual user signs in.
        if not self.access_token:
            return {"items": [], "next_cursor": None, "folder": None}
        n = clamp_mail_results(max_results)
        params, folder_row = self._query_params(None, folder, unread_only, received_after, received_before, n)
        page = self._page_messages(params, n, cursor)
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
        if not self.access_token or not (query or "").strip():
            return {"items": [], "next_cursor": None, "folder": None}
        n = clamp_mail_results(max_results)
        params, folder_row = self._query_params(query, folder, unread_only, received_after, received_before, n)
        page = self._page_messages(params, n, cursor)
        page["folder"] = folder_row
        return page

    def list_files(
        self,
        folder_id: Optional[str] = None,
        recursive: Optional[bool] = None,
    ) -> List[dict]:
        return self.list_messages(folder=folder_id)["items"]

    def search_files(self, query: str, max_results: Optional[int] = None, **_) -> List[dict]:
        return self.search_messages(query, max_results=max_results)["items"]

    @staticmethod
    def _decode_part(part: dict) -> str:
        data = (part.get("body") or {}).get("data")
        if not data:
            return ""
        try:
            padded = str(data) + ("=" * (-len(str(data)) % 4))
            raw = base64.urlsafe_b64decode(padded.encode())
        except (binascii.Error, ValueError, TypeError):
            return ""

        headers = GmailMailClient._header_map(part)
        content_type = headers.get("content-type", "")
        match = _CHARSET_RE.search(content_type)
        charset = match.group(1) if match else "utf-8"
        try:
            return raw.decode(charset, errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")

    @classmethod
    def _body_candidates(cls, part: dict) -> tuple[List[str], List[str]]:
        plain: List[str] = []
        html_parts: List[str] = []
        mime_type = str(part.get("mimeType") or "").lower()
        body = part.get("body") or {}
        is_attachment = bool(part.get("filename")) and bool(body.get("attachmentId"))

        if not is_attachment:
            decoded = cls._decode_part(part)
            if decoded:
                if mime_type == "text/plain":
                    plain.append(decoded)
                elif mime_type == "text/html":
                    html_parts.append(decoded)

        for child in part.get("parts") or []:
            child_plain, child_html = cls._body_candidates(child)
            plain.extend(child_plain)
            html_parts.extend(child_html)
        return plain, html_parts

    def read_file(self, file_id: str, **_) -> Any:
        message = self._get(
            f"/users/me/messages/{file_id}",
            params={"format": "full"},
        )
        payload = message.get("payload") or {}
        headers = self._header_map(payload)
        plain, html_parts = self._body_candidates(payload)
        if plain:
            body = "\n\n".join(part.strip() for part in plain if part.strip()).strip()
        elif html_parts:
            body = strip_html("\n".join(html_parts))
        else:
            body = str(message.get("snippet") or "").strip()

        # Same header-block link as the Outlook client: read_email backs both
        # mailboxes, so a message opened from Gmail has to carry its permalink
        # too. _web_url falls back to the message id when threadId is absent,
        # so this line is always populated here.
        return (
            f"Subject: {headers.get('subject') or '(no subject)'}\n"
            f"From: {headers.get('from', '')}\n"
            f"To: {headers.get('to', '')}\n"
            f"Date: {headers.get('date', '')}\n"
            f"Link: {self._web_url(message)}\n\n"
            f"{body}"
        )

    def test_connection(self) -> dict:
        if not self.access_token:
            return {
                "success": True,
                "message": "OAuth client saved. Have a user sign in with Google to access Gmail.",
            }
        try:
            profile = self._get("/users/me/profile")
            account = profile.get("emailAddress") or "Google account"
            return {"success": True, "message": f"Connected as {account}"}
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    # Mailboxes are always queried live per user; they have no shared admin
    # catalog and should never create one table row per message.
    @discovery_progress
    def get_schemas(self, *args, progress_callback=None, **kwargs) -> List:
        return []

    def get_schema(self, table_name: str):
        return None

    def prompt_schema(self) -> str:
        return "Gmail is searched and read live for the signed-in user."

    def execute_query(
        self,
        query: Optional[str] = None,
        table_name: Optional[str] = None,
        **kwargs,
    ):
        message_id = kwargs.get("message_id") or kwargs.get("file_id") or table_name
        if message_id:
            return self.read_file(str(message_id))
        if query:
            return self.search_files(query)
        return self.list_files()

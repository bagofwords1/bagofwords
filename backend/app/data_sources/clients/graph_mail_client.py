"""Microsoft Graph (Outlook) mail client — emails surfaced as readable "files".

Reuses GraphDriveClient's Entra OAuth (delegated per-user + service-principal
fallback) and HTTP plumbing. Each email is exposed through the existing
file-capability surface so the standard tools work over mail with no new
agent-tool surface:

  list_files   -> recent messages (id, subject, from, received)
  search_files -> Graph $search over messages
  read_file    -> the message rendered as plain text (headers + body)

The agent searches/reads email exactly like Drive/OneDrive files; the analysis
stack (read_file -> session file) materializes a message body when needed.
"""
from __future__ import annotations

import html
import re
import urllib.parse
from typing import Any, List, Optional

from app.data_sources.clients.base import Capability
from app.data_sources.clients.graph_drive_client import GraphDriveClient

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    s = _TAG_RE.sub(" ", s or "")
    s = html.unescape(s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


class GraphMailClient(GraphDriveClient):
    """Outlook/Exchange mail over Microsoft Graph, shaped as a file source."""

    capabilities = {Capability.LIST_FILES, Capability.READ_FILE, Capability.SEARCH_FILES}

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("mode", "outlook_mail")
        super().__init__(*args, **kwargs)

    @staticmethod
    def _addr(obj: dict) -> str:
        return (((obj or {}).get("emailAddress") or {}).get("address")) or ""

    def _msg_to_item(self, m: dict) -> dict:
        return {
            "id": m.get("id"),
            "name": m.get("subject") or "(no subject)",
            "mimeType": "message/rfc822",
            "from": self._addr(m.get("from")),
            "received": m.get("receivedDateTime"),
        }

    def list_files(self, folder_id: Optional[str] = None, recursive: Optional[bool] = None) -> List[dict]:
        data = self._get(
            "/me/messages?$top=25&$select=id,subject,from,receivedDateTime"
            "&$orderby=receivedDateTime%20desc"
        )
        return [self._msg_to_item(m) for m in (data.get("value") or [])]

    def search_files(self, query: str, **_) -> List[dict]:
        q = urllib.parse.quote(f'"{query}"')
        data = self._get(f"/me/messages?$search={q}&$top=25&$select=id,subject,from,receivedDateTime")
        return [self._msg_to_item(m) for m in (data.get("value") or [])]

    def read_file(self, file_id: str, **_) -> Any:
        m = self._get(
            f"/me/messages/{file_id}"
            "?$select=subject,from,toRecipients,receivedDateTime,body,bodyPreview"
        )
        frm = self._addr(m.get("from"))
        to = ", ".join(self._addr(r) for r in (m.get("toRecipients") or []))
        body = m.get("body") or {}
        content = body.get("content") or m.get("bodyPreview") or ""
        if (body.get("contentType") or "").lower() == "html":
            content = _strip_html(content)
        header = (
            f"Subject: {m.get('subject') or '(no subject)'}\n"
            f"From: {frm}\nTo: {to}\nDate: {m.get('receivedDateTime') or ''}\n\n"
        )
        return header + content

    # Email has no pre-indexed admin catalog — it's searched/read live per user.
    def get_schemas(self, *args, **kwargs) -> List:
        return []

    def test_connection(self) -> dict:
        try:
            me = self._get("/me?$select=userPrincipalName,displayName")
            who = me.get("userPrincipalName") or me.get("displayName") or "Microsoft account"
            return {"success": True, "message": f"Connected as {who}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

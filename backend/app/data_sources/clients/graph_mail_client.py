"""Microsoft Graph (Outlook) mail client with a mail-named agent surface.

Reuses GraphDriveClient's Entra OAuth (delegated per-user + service-principal
fallback) and HTTP plumbing. Message payloads reuse the file transport while
the client advertises distinct mail capabilities:

  list_emails  -> recent messages (id, subject, from, received, web link)
  search_email -> Graph $search over messages
  read_email   -> the message rendered as plain text (headers + link + body)

The shared execution layer still materializes message bodies as session files
when needed, but the planner and the user see email vocabulary throughout.
"""
from __future__ import annotations

import urllib.parse
from typing import Any, List, Optional

from app.data_sources.clients.base import Capability
from app.data_sources.clients.graph_drive_client import GraphDriveClient
from app.data_sources.clients.mail_common import strip_html


class GraphMailClient(GraphDriveClient):
    """Outlook/Exchange mail over Microsoft Graph, shaped as a file source.

    Declares the MAIL capabilities (not the file ones) so the agent surfaces the
    mail-named tools — ``list_emails`` / ``read_email`` / ``search_email`` —
    instead of ``list_files`` / ``read_file`` / ``search_files``. The underlying
    methods keep their file-tool names (``list_files``/``read_file``/
    ``search_files`` below) since the mail tools delegate straight to them; only
    the planner-facing tool vocabulary changes.
    """

    capabilities = {Capability.LIST_EMAILS, Capability.READ_EMAIL, Capability.SEARCH_EMAILS}

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
        }

    def list_files(self, folder_id: Optional[str] = None, recursive: Optional[bool] = None) -> List[dict]:
        # Mailbox enumeration goes through /me/messages, which only works with a
        # delegated user token. Without one (e.g. the admin's credential test, or
        # admin-save indexing before any user has signed in), return an empty
        # inventory rather than 400. The real enumeration runs per-user once a
        # user completes OAuth. Mirrors the OneDrive guard in the parent client.
        if not getattr(self, "_user_token_provided", True):
            return []
        data = self._get(
            "/me/messages?$top=25&$select=id,subject,from,receivedDateTime,webLink"
            "&$orderby=receivedDateTime%20desc"
        )
        return [self._msg_to_item(m) for m in (data.get("value") or [])]

    def search_files(self, query: str, **_) -> List[dict]:
        q = urllib.parse.quote(f'"{query}"')
        data = self._get(
            f"/me/messages?$search={q}&$top=25&$select=id,subject,from,receivedDateTime,webLink"
        )
        return [self._msg_to_item(m) for m in (data.get("value") or [])]

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
    def get_schemas(self, *args, **kwargs) -> List:
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

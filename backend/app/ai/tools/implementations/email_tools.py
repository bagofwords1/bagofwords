"""Provider-neutral, mail-named agent tools for Gmail and Outlook mailboxes.

Native mail clients expose ``list_files`` / ``read_file`` / ``search_files``
methods that return message-shaped results. Rather than expose the *file* tools
on a mailbox — which
made the planner reason about "files" and pick the wrong verb (e.g. loop
``search_files`` instead of opening a message) — we expose a distinct mail
vocabulary: ``list_emails`` / ``read_email`` / ``search_email``.

``read_email`` is a thin subclass of ``read_file`` (same resolution and
session-file materialization). ``list_emails`` / ``search_email`` have their
own run loop over the mail clients' ``list_messages`` / ``search_messages``:
a mailbox is browsed by folder, read state and received window, and must page
to completion rather than stop at the first page. All three gate on the mail
capabilities (``LIST_EMAILS`` / ``READ_EMAIL`` / ``SEARCH_EMAILS``).
Because mail clients advertise only the mail capabilities, a mailbox agent
sees ONLY these tools; a drive/SharePoint agent still sees the file tools; a
mixed agent sees both, each scoped to its own connection.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Optional, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.email_tools import (
    ListEmailsInput,
    ListEmailsOutput,
    MailFolderEntry,
    SearchEmailsInput,
    SearchEmailsOutput,
)
from app.ai.tools.schemas.file_tools import (
    FileEntry,
    ReadFileInput,
    ReadFileOutput,
)
from app.data_sources.clients.base import Capability

from .read_file import ReadFileTool

#: Preview characters rendered per row in the observation. Graph's bodyPreview
#: is 255 chars; this keeps a full page of rows inside one observation.
_OBS_PREVIEW_CHARS = 180


def _email_row(e: dict) -> str:
    from app.ai.tools.implementations._file_tool_common import email_inventory_row
    row = email_inventory_row(e)
    extras = []
    if e.get("is_read") is False:
        extras.append("UNREAD")
    if e.get("folder"):
        extras.append(f"in {e['folder']}")
    if extras:
        row += " (" + ", ".join(extras) + ")"
    preview = " ".join(str(e.get("preview") or "").split())
    if preview:
        if len(preview) > _OBS_PREVIEW_CHARS:
            preview = preview[: _OBS_PREVIEW_CHARS - 1] + "…"
        row += f"\n    {preview}"
    return row


def _folder_entry(row: Optional[dict]) -> Optional[dict]:
    if not row or not row.get("id"):
        return None
    return MailFolderEntry(
        id=str(row["id"]), name=str(row.get("name") or row["id"]),
        path=str(row.get("path") or row.get("name") or row["id"]),
        unread_count=row.get("unread_count"), total_count=row.get("total_count"),
    ).model_dump()


class _MailboxTool(Tool):
    """Shared run loop for list_emails / search_email.

    Every returned row is rendered into the observation (the planner reads the
    observation, not the output), and an incomplete result says so with the
    cursor to continue — the previous tools silently stopped at 25 messages,
    so a "summarise everything since yesterday" run reported a partial mailbox
    as if it were complete.
    """

    _required_capability: Capability
    _operation_name: str

    def _call(self, client, data) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def _scope(self, data, folder: Optional[dict]) -> str:
        bits = []
        if folder:
            bits.append(f"in {folder['path']}")
        if getattr(data, "unread_only", False):
            bits.append("unread")
        if getattr(data, "received_after", None):
            bits.append(f"received ≥ {data.received_after}")
        if getattr(data, "received_before", None):
            bits.append(f"received < {data.received_before}")
        return (" " + ", ".join(bits)) if bits else ""

    def _fail(self, data, msg: str) -> ToolEndEvent:
        out = {"success": False, "connection_id": data.connection_id, "error": msg}
        if getattr(data, "query", None) is not None:
            out["query"] = data.query
        return ToolEndEvent(type="tool.end", payload={
            "output": out, "observation": {"summary": msg, "success": False},
        })

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        from ._file_tool_common import friendly_tool_error, resolve_file_client

        data = self.input_model(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={
            "title": self._start_title(data),
            "connection_id": data.connection_id,
        })
        client, err = await resolve_file_client(runtime_ctx, data.connection_id, self._required_capability)
        if err:
            yield self._fail(data, err)
            return
        try:
            result = await self._call(client, data)
        except Exception as e:
            cname = getattr(getattr(client, "_bow_connection", None), "name", "") or ""
            yield self._fail(data, friendly_tool_error(self._operation_name, cname, e))
            return
        yield self._end(data, result)

    def _end(self, data, result: Dict[str, Any]) -> ToolEndEvent:
        if "folders" in result:
            folders = [f for f in (_folder_entry(r) for r in result["folders"]) if f]
            rows = []
            for f in folders:
                counts = []
                if f.get("unread_count") is not None:
                    counts.append(f"{f['unread_count']} unread")
                if f.get("total_count") is not None:
                    counts.append(f"{f['total_count']} total")
                rows.append(f"{f['path']}" + (f" ({', '.join(counts)})" if counts else "") + f" [id={f['id']}]")
            return ToolEndEvent(type="tool.end", payload={
                "output": {"success": True, "connection_id": data.connection_id,
                           "file_count": 0, "files": [], "folders": folders},
                "observation": {
                    "summary": f"Mailbox has {len(folders)} folder(s). Pass a path or id as folder_id.",
                    "details": "\n".join(rows),
                    "success": True,
                },
            })

        entries = [FileEntry(
            id=f.get("id"), name=f.get("name") or "(no subject)",
            path=f.get("path") if isinstance(f.get("path"), str) else None,
            mime_type=f.get("mime_type"), size=f.get("size"),
            modified_at=f.get("modified_at"),
            sender=f.get("sender") or f.get("from"),
            web_url=f.get("web_url"),
            preview=f.get("preview"), is_read=f.get("is_read"), folder=f.get("folder"),
        ).model_dump() for f in result.get("items") or []]
        folder = _folder_entry(result.get("folder"))
        cursor = result.get("next_cursor")
        summary = f"{self._verb(data)} {len(entries)} email(s){self._scope(data, folder)}"
        if folder and folder.get("total_count") is not None:
            summary += (
                f" — folder holds {folder['total_count']} message(s)"
                + (f", {folder['unread_count']} unread" if folder.get("unread_count") is not None else "")
            )
        if cursor:
            summary += (
                f". INCOMPLETE: more messages match. Call {self._operation_name} again with "
                f'cursor="{cursor}" (keep going until no cursor is returned) before '
                "concluding anything about the whole set."
            )
        else:
            summary += ". Complete: these are all the matching messages."
        observation: Dict[str, Any] = {"summary": summary, "success": True}
        if entries:
            observation["details"] = "\n".join(_email_row(e) for e in entries)
        output: Dict[str, Any] = {
            "success": True,
            "connection_id": data.connection_id,
            "file_count": len(entries),
            "files": entries,
            "truncated": bool(cursor),
            "next_cursor": cursor,
            "folder": folder,
        }
        if getattr(data, "query", None) is not None:
            output["query"] = data.query
        return ToolEndEvent(type="tool.end", payload={"output": output, "observation": observation})


class ListEmailsTool(_MailboxTool):
    """List messages in a Gmail or Outlook mailbox (or its folders)."""

    _required_capability = Capability.LIST_EMAILS
    _operation_name = "list_emails"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="list_emails",
            description=(
                "List emails in a Gmail or Outlook / Microsoft 365 mailbox, newest "
                "first. Filter server-side with folder_id (e.g. 'Ops', "
                "'Inbox/Telex In'), unread_only, received_after / received_before "
                "(ISO-8601). Each row has id, subject, sender, received time, "
                "read state, folder and a body preview — usually enough to triage "
                "without opening the message. Results are paged: when the summary "
                "says INCOMPLETE, call again with the given cursor until no cursor "
                "is returned — never summarise a window you have not fully listed. "
                "list_folders=true returns the folder tree with unread counts. "
                "Open a message with `read_email`; find by keyword with `search_email`."
            ),
            category="research",
            input_schema=ListEmailsInput.model_json_schema(),
            output_schema=ListEmailsOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=90,
            tags=["email", "gmail", "outlook", "mail", "inbox", "list", "folder", "unread"],
            requires_capability="list_emails",
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ListEmailsInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ListEmailsOutput

    def _start_title(self, data) -> str:
        return "Listing mail folders" if data.list_folders else "Listing emails"

    def _verb(self, data) -> str:
        return "Listed"

    async def _call(self, client, data) -> Dict[str, Any]:
        import asyncio
        if data.list_folders:
            return {"folders": await asyncio.to_thread(client.list_mail_folders)}
        return await asyncio.to_thread(
            client.list_messages,
            folder=data.folder_id, unread_only=data.unread_only,
            received_after=data.received_after, received_before=data.received_before,
            max_results=data.max_results, cursor=data.cursor,
        )


class ReadEmailTool(ReadFileTool):
    """Read a full email/message from a Gmail or Outlook mailbox by its id."""

    _required_capability = Capability.READ_EMAIL
    _start_noun = "email"
    _operation_name = "read_email"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_email",
            description=(
                "Read a full email from a Gmail or Outlook / Microsoft 365 mailbox and "
                "attach it to the conversation. Pass the message id (from "
                "`list_emails` or `search_email`) as `file_id`, and the mailbox "
                "connection as `connection_id`. Returns the message headers "
                "(subject, from, to, date, and a `Link:` line with the message's "
                "URL in Gmail or Outlook on the web) plus the body as plain "
                "text, so you can quote and analyse it directly — cite the "
                "`Link:` URL when pointing the user at the message, never the "
                "opaque id. USE THIS — not read_file — to open a message "
                "surfaced by list_emails / search_email."
            ),
            category="research",
            input_schema=ReadFileInput.model_json_schema(),
            output_schema=ReadFileOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=60,
            tags=["email", "gmail", "outlook", "mail", "message", "read"],
            requires_capability="read_email",
        )


class SearchEmailsTool(_MailboxTool):
    """Search a Gmail or Outlook mailbox using its provider-native query."""

    _required_capability = Capability.SEARCH_EMAILS
    _operation_name = "search_email"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="search_email",
            description=(
                "Search a Gmail or Outlook / Microsoft 365 mailbox with a "
                "provider-native query. Outlook takes KQL over subject, body and "
                "sender (`from:`, `subject:`, `OR`, quoted phrases); Gmail takes "
                "inbox syntax (`from:`, `has:attachment`). Narrow with folder_id, "
                "unread_only and received_after / received_before rather than "
                "query terms. Rows carry sender, received time, read state and a "
                "body preview. Paged like list_emails: follow the cursor until none "
                "is returned. To cover EVERY message in a window, prefer "
                "list_emails with received_after — keyword searches miss messages "
                "that don't use the keyword. Open one with `read_email`."
            ),
            category="research",
            input_schema=SearchEmailsInput.model_json_schema(),
            output_schema=SearchEmailsOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=90,
            tags=["email", "gmail", "outlook", "mail", "message", "search"],
            requires_capability="search_emails",
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return SearchEmailsInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return SearchEmailsOutput

    def _start_title(self, data) -> str:
        return f"Searching emails: {data.query!r}"

    def _verb(self, data) -> str:
        return f"Search {data.query!r} found"

    async def _call(self, client, data) -> Dict[str, Any]:
        import asyncio
        return await asyncio.to_thread(
            client.search_messages, data.query,
            folder=data.folder_id, unread_only=data.unread_only,
            received_after=data.received_after, received_before=data.received_before,
            max_results=data.max_results, cursor=data.cursor,
        )

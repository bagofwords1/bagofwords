"""Mail-named agent tools for Outlook / Microsoft 365 mailboxes.

Outlook mail is surfaced over Microsoft Graph by ``GraphMailClient``, whose
``list_files`` / ``read_file`` / ``search_files`` methods already return
message-shaped results. Rather than expose the *file* tools on a mailbox — which
made the planner reason about "files" and pick the wrong verb (e.g. loop
``search_files`` instead of opening a message) — we expose a distinct mail
vocabulary: ``list_emails`` / ``read_email`` / ``search_email``.

These are thin subclasses of the file tools: they reuse the exact same
resolution, live-fetch, and (for read) session-file materialization logic, and
only change the planner-facing name/description and the capability the resolved
connection must expose (``LIST_EMAILS`` / ``READ_EMAIL`` / ``SEARCH_EMAILS``).
Because the mail client advertises only the mail capabilities, an Outlook agent
sees ONLY these tools; a drive/SharePoint agent still sees the file tools; a
mixed agent sees both, each scoped to its own connection.
"""
from __future__ import annotations

from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.file_tools import (
    ListFilesInput,
    ListFilesOutput,
    ReadFileInput,
    ReadFileOutput,
    SearchFilesInput,
    SearchFilesOutput,
)
from app.data_sources.clients.base import Capability

from .list_files import ListFilesTool
from .read_file import ReadFileTool
from .search_files import SearchFilesTool


class ListEmailsTool(ListFilesTool):
    """List recent messages in an Outlook mailbox connection."""

    _required_capability = Capability.LIST_EMAILS

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="list_emails",
            description=(
                "List recent emails in an Outlook / Microsoft 365 mailbox "
                "connection. Returns messages with their id, subject, sender and "
                "received time (most recent first). Use `read_email` to open one "
                "message by its id, or `search_email` to find messages by "
                "keyword. `connection_id` is the mailbox connection (id or name)."
            ),
            category="research",
            input_schema=ListFilesInput.model_json_schema(),
            output_schema=ListFilesOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=30,
            tags=["email", "outlook", "mail", "inbox", "list"],
            requires_capability="list_emails",
        )


class ReadEmailTool(ReadFileTool):
    """Read a full email/message from an Outlook mailbox by its id."""

    _required_capability = Capability.READ_EMAIL

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_email",
            description=(
                "Read a full email from an Outlook / Microsoft 365 mailbox and "
                "attach it to the conversation. Pass the message id (from "
                "`list_emails` or `search_email`) as `file_id`, and the mailbox "
                "connection as `connection_id`. Returns the message headers "
                "(subject, from, to, date) plus the body as plain text, so you "
                "can quote and analyse it directly. USE THIS — not read_file — "
                "to open a message surfaced by list_emails / search_email."
            ),
            category="research",
            input_schema=ReadFileInput.model_json_schema(),
            output_schema=ReadFileOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=60,
            tags=["email", "outlook", "mail", "message", "read"],
            requires_capability="read_email",
        )


class SearchEmailsTool(SearchFilesTool):
    """Search an Outlook mailbox (Graph $search over subject / body / sender)."""

    _required_capability = Capability.SEARCH_EMAILS

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="search_email",
            description=(
                "Search an Outlook / Microsoft 365 mailbox for messages matching "
                "a query (Graph $search over subject, body and sender). Returns "
                "matching messages (id, subject, from, received); open the most "
                "relevant one with `read_email`. `connection_id` is the mailbox "
                "connection. Prefer this over listing when the user names a "
                "topic, person, or keyword."
            ),
            category="research",
            input_schema=SearchFilesInput.model_json_schema(),
            output_schema=SearchFilesOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=30,
            tags=["email", "outlook", "mail", "message", "search"],
            requires_capability="search_emails",
        )

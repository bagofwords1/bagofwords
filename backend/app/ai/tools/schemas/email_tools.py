"""Input/output schemas for the mailbox tools (list_emails / search_email).

Separate from the file-tool schemas because a mailbox is browsed by folder,
read state and received window, and must page to completion: a daily digest
over a busy inbox is wrong if it silently stops at the first page.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from app.ai.tools.schemas.file_tools import FileEntry, _title_field
from app.data_sources.clients.mail_common import DEFAULT_MAIL_RESULTS, MAX_MAIL_RESULTS

_CONNECTION_DESC = (
    "The mailbox connection: the `id=` of its `<connection>` tag in the schema "
    "(the agent's id or the connection's name are also accepted)."
)
_FOLDER_DESC = (
    "Optional mail folder to stay inside — its path ('Inbox/Telex In'), its "
    "unique display name ('Ops'), or an id from list_emails(list_folders=true). "
    "Gmail labels work the same way. Omit to cover the whole mailbox."
)
_MAX_DESC = (
    f"Max messages to return in this call (default {DEFAULT_MAIL_RESULTS}, max "
    f"{MAX_MAIL_RESULTS}). When more match, `next_cursor` is set — call again "
    "with it until it comes back empty to see every message."
)
_CURSOR_DESC = (
    "Continue a previous call: pass its `next_cursor` exactly. The cursor "
    "remembers that call's folder/filters/query; other arguments are ignored."
)


class _MailFilterFields(BaseModel):
    connection_id: str = Field(..., description=_CONNECTION_DESC)
    folder_id: Optional[str] = Field(None, description=_FOLDER_DESC)
    unread_only: bool = Field(False, description="Only messages not yet marked read.")
    received_after: Optional[str] = Field(
        None,
        description="Only messages received at/after this time, ISO-8601 ('2026-10-05' or '2026-10-05T06:00:00Z'; no offset = UTC).",
    )
    received_before: Optional[str] = Field(
        None, description="Only messages received before this time (ISO-8601, as received_after)."
    )
    max_results: int = Field(DEFAULT_MAIL_RESULTS, ge=1, le=MAX_MAIL_RESULTS, description=_MAX_DESC)
    cursor: Optional[str] = Field(None, description=_CURSOR_DESC)
    title: Optional[str] = _title_field()


class ListEmailsInput(_MailFilterFields):
    list_folders: bool = Field(
        False,
        description=(
            "Return the mailbox's folders (path, unread and total counts) instead "
            "of messages — use it to find a folder the user named."
        ),
    )


class SearchEmailsInput(_MailFilterFields):
    query: str = Field(
        ...,
        description=(
            "Provider-native query. Outlook: KQL over subject/body/sender, e.g. "
            "`from:alice subject:report`, `breakdown OR \"main engine\"`. Gmail: "
            "inbox syntax, e.g. `from:finance has:attachment`. Use folder_id / "
            "unread_only / received_* for folder, read state and dates — not query terms."
        ),
    )


class MailFolderEntry(BaseModel):
    id: str
    name: str
    path: str
    unread_count: Optional[int] = None
    total_count: Optional[int] = None


class ListEmailsOutput(BaseModel):
    success: bool
    connection_id: str
    file_count: int = 0
    files: List[FileEntry] = Field(default_factory=list)
    truncated: bool = Field(False, description="True when more messages match than were returned — see next_cursor.")
    next_cursor: Optional[str] = None
    folder: Optional[MailFolderEntry] = None
    folders: Optional[List[MailFolderEntry]] = None
    error: Optional[str] = None


class SearchEmailsOutput(ListEmailsOutput):
    query: str = ""

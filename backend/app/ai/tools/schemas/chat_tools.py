"""Input/output schemas for the chat tools (list_chat_messages /
search_chat_messages). ``read_chat_thread`` reuses the read_file schemas, the
same way read_email does.

A chat source is browsed by conversation (a Teams channel or chat), sender and
time window, and pages to completion like a mailbox.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from app.ai.tools.schemas.file_tools import FileEntry, _title_field
from app.data_sources.clients.mail_common import DEFAULT_MAIL_RESULTS, MAX_MAIL_RESULTS

_CONNECTION_DESC = (
    "The chat connection (e.g. Microsoft Teams): the `id=` of its `<connection>` "
    "tag in the schema (the agent's id or the connection's name are also accepted)."
)
_CONVERSATION_DESC = (
    "Optional conversation to stay inside: a channel ('General', 'Sales › General'), "
    "a chat's topic ('Forecast 2027'), a person you chat with ('Dana'), or an id "
    "from list_chat_messages(list_conversations=true). Omit to cover every "
    "channel and chat the user can see."
)
_MAX_DESC = (
    f"Max messages to return in this call (default {DEFAULT_MAIL_RESULTS}, max "
    f"{MAX_MAIL_RESULTS}). When more match, `next_cursor` is set — call again "
    "with it until it comes back empty to see every message."
)
_CURSOR_DESC = (
    "Continue a previous call: pass its `next_cursor` exactly, with the same "
    "other arguments."
)


class _ChatFilterFields(BaseModel):
    connection_id: str = Field(..., description=_CONNECTION_DESC)
    conversation: Optional[str] = Field(None, description=_CONVERSATION_DESC)
    from_user: Optional[str] = Field(
        None, description="Only messages from this person (name or email, partial match)."
    )
    sent_after: Optional[str] = Field(
        None, description="Only messages sent at/after this ISO-8601 date or time (e.g. 2026-10-01)."
    )
    sent_before: Optional[str] = Field(
        None, description="Only messages sent before this ISO-8601 date or time."
    )
    max_results: int = Field(DEFAULT_MAIL_RESULTS, ge=1, le=MAX_MAIL_RESULTS, description=_MAX_DESC)
    cursor: Optional[str] = Field(None, description=_CURSOR_DESC)
    title: Optional[str] = _title_field()


class ListChatMessagesInput(_ChatFilterFields):
    list_conversations: bool = Field(
        False,
        description=(
            "Return the channels and chats the user can see (with ids) instead of "
            "messages. Use it to find the exact conversation to browse."
        ),
    )


class SearchChatMessagesInput(_ChatFilterFields):
    query: str = Field(
        ...,
        min_length=1,
        description=(
            "What to look for — keywords or a short phrase (e.g. 'Q3 churn', "
            "'pricing change'). Matched by Microsoft Search across message text."
        ),
    )


class ConversationEntry(BaseModel):
    id: str
    name: str
    kind: Optional[str] = Field(None, description="channel | chat | group chat | meeting chat")
    last_activity: Optional[str] = None
    web_url: Optional[str] = None


class ChatMessageEntry(FileEntry):
    reply_count: Optional[int] = Field(
        None, description="For a channel post, how many replies its thread has."
    )
    is_reply: Optional[bool] = Field(None, description="True when the message is a reply in a thread.")
    thread: Optional[str] = Field(
        None, description="For a reply, the post it answers (sender: text) — the start of its thread."
    )
    context: Optional[List[str]] = Field(
        None,
        description=(
            "Neighbouring messages of a search hit (↑ before, ↓ after), so an answer that does "
            "not repeat the searched words is still visible. Read the thread for the full discussion."
        ),
    )
    more_after: Optional[int] = Field(
        None, description="How many later messages of the same discussion are not shown in `context`."
    )


class ListChatMessagesOutput(BaseModel):
    success: bool
    connection_id: str
    file_count: int = 0
    files: List[ChatMessageEntry] = Field(default_factory=list)
    truncated: bool = Field(False, description="True when more messages match than were returned — see next_cursor.")
    next_cursor: Optional[str] = None
    folder: Optional[ConversationEntry] = None
    folders: Optional[List[ConversationEntry]] = None
    error: Optional[str] = None


class SearchChatMessagesOutput(ListChatMessagesOutput):
    query: str = ""
    broadened_query: Optional[str] = Field(
        None, description="Set when no message matched every word and the search was retried with ANY of them."
    )

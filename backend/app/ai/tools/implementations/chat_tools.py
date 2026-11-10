"""Chat-named agent tools for chat knowledge sources (Microsoft Teams).

Same design as the email tools: a chat source must not be reasoned about as
"files", so it gets its own vocabulary —

  search_chat_messages -> find what was said, across every channel and chat
  list_chat_messages   -> browse one conversation or a time window; with
                          ``list_conversations=true``, the channels and chats
  read_chat_thread     -> the whole thread around a message (thin subclass of
                          read_file: same resolution, session-file
                          materialization and citation link)

All three gate on the chat capabilities, which only chat clients declare, so a
Teams agent sees ONLY these tools and a drive or mailbox agent never sees them.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, Optional, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.chat_tools import (
    ChatMessageEntry,
    ConversationEntry,
    ListChatMessagesInput,
    ListChatMessagesOutput,
    SearchChatMessagesInput,
    SearchChatMessagesOutput,
)
from app.ai.tools.schemas.file_tools import ReadFileInput, ReadFileOutput
from app.data_sources.clients.base import Capability

from .read_file import ReadFileTool

#: Preview characters rendered per row in the observation.
_OBS_PREVIEW_CHARS = 220


def _when(value: Optional[str]) -> str:
    text = str(value or "")
    # 2026-11-07T12:06:46.123Z -> 2026-11-07 12:06
    return text[:16].replace("T", " ") if len(text) >= 16 else text


def _message_row(e: dict) -> str:
    """One observation line: when · where · who, then the text."""
    bits = [_when(e.get("modified_at"))]
    if e.get("folder"):
        bits.append(str(e["folder"]))
    who = e.get("sender") or "Unknown"
    head = " · ".join(b for b in bits if b) + f" · {who}"
    if e.get("is_reply"):
        head += " (reply)"
    if e.get("reply_count"):
        head += f" ({e['reply_count']} repl{'y' if e['reply_count'] == 1 else 'ies'})"
    text = " ".join(str(e.get("preview") or "").split())
    if len(text) > _OBS_PREVIEW_CHARS:
        text = text[: _OBS_PREVIEW_CHARS - 1] + "…"
    row = f"- [id={e.get('id')}] {head}\n    {text}"
    if e.get("thread"):
        row += f"\n    in reply to {e['thread']}"
    for line in e.get("context") or []:
        row += f"\n      {line}"
    if e.get("more_after"):
        row += (f"\n      … {e['more_after']} more message(s) after — read_chat_thread "
                "to see how the discussion ended")
    return row


def _conversation(row: Optional[dict]) -> Optional[dict]:
    if not row or not row.get("id"):
        return None
    return ConversationEntry(
        id=str(row["id"]), name=str(row.get("name") or row["id"]),
        kind=row.get("kind"), last_activity=row.get("last_activity"),
        web_url=row.get("web_url"),
    ).model_dump()


class _ChatTool(Tool):
    """Shared run loop for list_chat_messages / search_chat_messages."""

    _required_capability: Capability
    _operation_name: str

    def _call(self, client, data) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def _scope(self, data, folder: Optional[dict]) -> str:
        bits = []
        if folder:
            bits.append(f"in {folder['name']}")
        if getattr(data, "from_user", None):
            bits.append(f"from {data.from_user}")
        if getattr(data, "sent_after", None):
            bits.append(f"sent ≥ {data.sent_after}")
        if getattr(data, "sent_before", None):
            bits.append(f"sent < {data.sent_before}")
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
        if "conversations" in result:
            convs = [c for c in (_conversation(r) for r in result["conversations"]) if c]
            rows = [f"{c['name']} ({c.get('kind') or 'conversation'}) [id={c['id']}]" for c in convs]
            return ToolEndEvent(type="tool.end", payload={
                "output": {"success": True, "connection_id": data.connection_id,
                           "file_count": 0, "files": [], "folders": convs},
                "observation": {
                    "summary": (
                        f"The user can see {len(convs)} conversation(s). Pass a name or id "
                        "as `conversation` to list_chat_messages / search_chat_messages."
                    ),
                    "details": "\n".join(rows),
                    "success": True,
                },
            })

        raw = result.get("items") or []
        entries = [ChatMessageEntry(
            id=f.get("id"), name=f.get("name") or "(no text)",
            # The card shows `path` beside the name: the conversation reads
            # better there than the browse path (conversation/sender: text).
            path=f.get("folder"),
            mime_type=f.get("mime_type"),
            modified_at=f.get("modified_at"),
            sender=f.get("sender"),
            web_url=f.get("web_url"),
            preview=f.get("preview"), folder=f.get("folder"),
            reply_count=f.get("reply_count"), is_reply=f.get("is_reply"),
            thread=f.get("thread"), context=f.get("context"), more_after=f.get("more_after"),
        ).model_dump() for f in raw]
        folder = _conversation(result.get("folder"))
        cursor = result.get("next_cursor")
        summary = f"{self._verb(data)} {len(entries)} message(s){self._scope(data, folder)}"
        if result.get("broadened_query"):
            summary += (
                f" (no message contained every word, so this searched for ANY of them: "
                f"{result['broadened_query']} — ranked by relevance; judge each hit by its text)"
            )
        if cursor:
            summary += (
                f". INCOMPLETE: more messages match. Call {self._operation_name} again with "
                f'cursor="{cursor}" (keep going until no cursor is returned) before '
                "concluding anything about the whole set."
            )
        else:
            summary += ". Complete: these are all the matching messages."
        partial = result.get("partial") or []
        if partial:
            summary += (
                " Only the newest messages of each conversation were scanned; these have "
                f"older ones: {', '.join(partial[:8])}. Pass `conversation` to page through one."
            )
        if entries:
            summary += " Read a hit with read_chat_thread to see the whole thread before answering."
        observation: Dict[str, Any] = {"summary": summary, "success": True}
        if entries:
            observation["details"] = "\n".join(_message_row(e) for e in entries)
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
        if result.get("broadened_query"):
            output["broadened_query"] = result["broadened_query"]
        return ToolEndEvent(type="tool.end", payload={"output": output, "observation": observation})


class ListChatMessagesTool(_ChatTool):
    """Browse messages in one Teams conversation, or the newest across all."""

    _required_capability = Capability.LIST_CHAT_MESSAGES
    _operation_name = "list_chat_messages"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="list_chat_messages",
            description=(
                "List chat messages (Microsoft Teams channels and chats) newest first, "
                "read live as the signed-in user. Narrow with `conversation` (a "
                "channel like 'General', a chat topic, or a person), `from_user`, "
                "and `sent_after` / `sent_before` (ISO-8601). Channel posts are "
                "followed by their replies. Each row has id, conversation, sender, "
                "time and the message text. Results are paged: when the summary says "
                "INCOMPLETE, call again with the cursor. list_conversations=true "
                "returns the channels and chats the user can see. To find WHAT was "
                "said about a topic use `search_chat_messages`; open the full thread "
                "around a message with `read_chat_thread`."
            ),
            category="research",
            input_schema=ListChatMessagesInput.model_json_schema(),
            output_schema=ListChatMessagesOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=90,
            tags=["teams", "chat", "messages", "channel", "conversation", "list"],
            requires_capability="list_chat_messages",
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ListChatMessagesInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ListChatMessagesOutput

    def _start_title(self, data) -> str:
        if data.list_conversations:
            return "Listing conversations"
        return f"Listing messages in {data.conversation}" if data.conversation else "Listing messages"

    def _verb(self, data) -> str:
        return "Listed"

    async def _call(self, client, data) -> Dict[str, Any]:
        import asyncio
        if data.list_conversations:
            return {"conversations": await asyncio.to_thread(client.list_conversations)}
        return await asyncio.to_thread(
            client.list_messages,
            conversation=data.conversation, from_user=data.from_user,
            sent_after=data.sent_after, sent_before=data.sent_before,
            max_results=data.max_results, cursor=data.cursor,
        )


class SearchChatMessagesTool(_ChatTool):
    """Search every Teams channel and chat message the user can see."""

    _required_capability = Capability.SEARCH_CHAT_MESSAGES
    _operation_name = "search_chat_messages"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="search_chat_messages",
            description=(
                "Search chat messages (Microsoft Teams) across every channel and chat "
                "the signed-in user can see — Microsoft Search enforces their access, "
                "so private chats they are not in never appear. Use keywords or a "
                "short phrase ('Q3 churn', 'pricing change'); try 2-3 phrasings when "
                "the first finds nothing. Narrow with `conversation`, `from_user` and "
                "`sent_after` / `sent_before` rather than query words. Hits show "
                "conversation, sender, time and the matching text — but a hit is ONE "
                "line of a discussion: read it with `read_chat_thread` to see the "
                "replies (corrections and final decisions are often there) before "
                "answering. Paged: follow the cursor until none is returned."
            ),
            category="research",
            input_schema=SearchChatMessagesInput.model_json_schema(),
            output_schema=SearchChatMessagesOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=90,
            tags=["teams", "chat", "messages", "channel", "search", "slack"],
            requires_capability="search_chat_messages",
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return SearchChatMessagesInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return SearchChatMessagesOutput

    def _start_title(self, data) -> str:
        return f"Searching messages: {data.query!r}"

    def _verb(self, data) -> str:
        return f"Search {data.query!r} found"

    async def _call(self, client, data) -> Dict[str, Any]:
        import asyncio
        return await asyncio.to_thread(
            client.search_messages, data.query,
            conversation=data.conversation, from_user=data.from_user,
            sent_after=data.sent_after, sent_before=data.sent_before,
            max_results=data.max_results, cursor=data.cursor,
        )


class ReadChatThreadTool(ReadFileTool):
    """Read the whole thread (or chat window) around one Teams message."""

    _required_capability = Capability.READ_CHAT_THREAD
    _start_noun = "thread"
    _operation_name = "read_chat_thread"
    # Read live, shown to the agent, never kept as a report file (see ReadFileTool).
    _keep_session_copy = False

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_chat_thread",
            description=(
                "Read the whole conversation around a chat message (Microsoft Teams) "
                "and attach it to the conversation. Pass the message id from "
                "`search_chat_messages` / `list_chat_messages` as `file_id`, and the "
                "chat connection as `connection_id`. A channel message returns its "
                "entire thread (the post and every reply); a chat message returns the "
                "messages around it. The requested message is marked ▶. The header "
                "has a `Link:` line — cite it as a markdown link with a short label "
                "(e.g. `[Q3 revenue thread](<Link>)`), never a bare URL or the opaque id. Passing a conversation id (from "
                "list_conversations) returns its newest messages. USE THIS — not "
                "read_file — for chat messages."
            ),
            category="research",
            input_schema=ReadFileInput.model_json_schema(),
            output_schema=ReadFileOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=60,
            tags=["teams", "chat", "thread", "messages", "read"],
            requires_capability="read_chat_thread",
        )

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        # The read path treats an id with "/" as a file path, so the card would
        # be titled with the bare message id and show the whole id as its path.
        # Name the read after the conversation instead (the thread header's
        # first line, e.g. "Teams channel: Sales › General").
        async for event in super().run_stream(tool_input, runtime_ctx):
            if getattr(event, "type", None) == "tool.end":
                out = (event.payload or {}).get("output") or {}
                where = _conversation_from_thread_text(out.get("text") or "")
                if out.get("success") and where:
                    out["file_name"] = where
                    out["path"] = where
            yield event


def _conversation_from_thread_text(text: str) -> Optional[str]:
    """"Sales › General" from the "Teams channel: Sales › General" header line."""
    first = (text or "").lstrip().split("\n", 1)[0].strip()
    if first.startswith("Teams ") and ": " in first:
        return first.split(": ", 1)[1].strip() or None
    return None


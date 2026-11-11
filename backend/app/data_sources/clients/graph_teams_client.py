"""Microsoft Teams messages over Microsoft Graph, read live with the user's token.

A knowledge source in the same shape as the Outlook mail client: nothing is
indexed or stored, every call is a live Graph request made AS the signed-in
user, so a user only ever sees the channels and chats they are a member of.

The agent surface is a chat vocabulary, not a file one:

  search_chat_messages -> Graph Search (``chatMessage``) across every channel
                          and chat the user can see, paged
  list_chat_messages   -> messages in one conversation (or the newest across
                          all of them), newest first, filtered by window and
                          sender; ``list_conversations`` lists the channels and
                          chats themselves
  read_chat_thread     -> the whole thread around one message: a channel post
                          with all its replies, or the surrounding window of a
                          chat

Ids are readable and self-describing so a hit from search can be read without
any extra lookup state:

  conversation: ``channel:<teamId>/<channelId>``   or ``chat:<chatId>``
  message:      ``channel:<teamId>/<channelId>/<messageId>``
                ``chat:<chatId>/<messageId>``

Graph ids never contain ``/``, so the split is unambiguous.
"""
from __future__ import annotations

import logging
import re
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.data_sources.clients.base import Capability
from app.data_sources.clients.graph_drive_client import GRAPH_BASE, GraphDriveClient
from app.data_sources.clients.mail_common import (
    clamp_mail_results,
    decode_cursor,
    encode_cursor,
    parse_mail_datetime,
    strip_html,
)
from app.data_sources.clients.progress import discovery_progress

logger = logging.getLogger(__name__)

# Graph serves at most 50 messages per page for both channel and chat listings.
_PAGE_SIZE = 50
# Graph Search pages chatMessage hits 25 at a time.
_SEARCH_PAGE = 25
# Safety valves on one call's page walk.
_MAX_PAGES = 20
_MAX_SEARCH_PAGES = 20
# Bound on how many channels + chats one listing will look at.
_MAX_CONVERSATIONS = 300
# Fan-out listing (no conversation named): pages read per conversation.
_FANOUT_PAGES = 2
_FANOUT_WORKERS = 6
# Pages scanned per channel / chat to give search hits their surrounding context.
_SEARCH_THREAD_PAGES = 2
# Neighbouring messages attached to each search hit (Glean-style hit context).
_CONTEXT_BEFORE = 2
_CONTEXT_AFTER = 3
_CONTEXT_CHARS = 200
# Messages of context shown on each side of a chat message on read.
_CHAT_CONTEXT = 12
# Root pages scanned to find the parent of a channel reply (search hits do not
# carry it). Ids are millisecond timestamps, so the scan stops early.
_MAX_ROOT_SCAN_PAGES = 10
_PREVIEW_CHARS = 255

_HIGHLIGHT_RE = re.compile(r"</?c\d+>")
# Words that never decide a match. Microsoft Search ANDs every word of a plain
# query, so "what drove Q3 revenue growth" finds nothing unless one message
# says all of it; the broadened retry ORs the remaining words instead.
_STOPWORDS = frozenset(
    "a an and are as at be by did do does for from how in is it of on or our "
    "that the this to was we were what when where which who why with".split()
)
_KQL_SYNTAX_RE = re.compile(r'["():]|\b(?:AND|OR|NOT|NEAR)\b')


def broadened_query(query: str) -> Optional[str]:
    """``a OR b OR c`` from a plain multi-word query, or None when there is
    nothing to broaden (one word, or the caller already wrote KQL)."""
    q = (query or "").strip()
    if _KQL_SYNTAX_RE.search(q):
        return None
    words = [w for w in re.findall(r"[\w$%.'-]+", q) if w.lower().strip(".'-") not in _STOPWORDS]
    words = list(dict.fromkeys(w.strip(".'-") for w in words if w.strip(".'-")))
    return " OR ".join(words) if len(words) >= 2 else None
_PATH_UNSAFE = str.maketrans({"/": "∕", "\n": " ", "\r": " "})


def _dt(value: Optional[str]) -> Optional[datetime]:
    try:
        return parse_mail_datetime(value)
    except ValueError:
        return None


def _snippet(text: str, n: int = 60) -> str:
    one = " ".join((text or "").split())
    return one if len(one) <= n else one[: n - 1] + "…"


def _fmt_time(value: Optional[str]) -> str:
    moment = _dt(value)
    return moment.strftime("%Y-%m-%d %H:%M UTC") if moment else (value or "")


def parse_teams_id(value: str) -> Dict[str, Optional[str]]:
    """Split a conversation or message id into its parts.

    Returns ``{kind, team_id, channel_id, chat_id, message_id}``; raises
    ValueError for anything that is not a Teams id this client issued.
    """
    raw = str(value or "").strip()
    kind, _, rest = raw.partition(":")
    parts = [p for p in rest.split("/")] if rest else []
    if kind == "channel" and len(parts) in (2, 3) and all(parts):
        return {"kind": "channel", "team_id": parts[0], "channel_id": parts[1],
                "chat_id": None, "message_id": parts[2] if len(parts) == 3 else None}
    if kind == "chat" and len(parts) in (1, 2) and all(parts):
        return {"kind": "chat", "team_id": None, "channel_id": None,
                "chat_id": parts[0], "message_id": parts[1] if len(parts) == 2 else None}
    raise ValueError(
        f"{raw!r} is not a Teams conversation or message id. Pass an id exactly as "
        "returned by search_chat_messages / list_chat_messages (e.g. "
        "'channel:<team>/<channel>/<message>' or 'chat:<chat>/<message>')."
    )


class GraphTeamsClient(GraphDriveClient):
    """Teams channels and chats, searched and read live as the signed-in user."""

    capabilities = {
        Capability.LIST_CHAT_MESSAGES,
        Capability.READ_CHAT_THREAD,
        Capability.SEARCH_CHAT_MESSAGES,
    }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("mode", "teams_messages")
        super().__init__(*args, **kwargs)
        self._conversations: Optional[List[dict]] = None
        self._conv_lock = threading.Lock()
        self._me: Optional[dict] = None
        self._root_cache: Dict[str, str] = {}
        # (team, channel) -> {message id: root message (with "replies")}, built
        # from one walk of the channel's root posts with replies expanded.
        self._thread_index: Dict[Tuple[str, str], Dict[str, dict]] = {}
        # chat id -> its newest messages, oldest first (search-hit context).
        self._chat_recent: Dict[str, List[dict]] = {}

    @property
    def description(self) -> str:
        return "Microsoft Teams channels and chats the signed-in user can see"

    # ------------------------------------------------------------- identity

    def _whoami(self) -> dict:
        if self._me is None:
            self._me = self._get("/me?$select=id,displayName,userPrincipalName")
        return self._me

    # -------------------------------------------------------- conversations

    def list_conversations(self) -> List[dict]:
        """Every channel (in teams the user joined) and chat the user is in.

        Rows: ``{id, name, kind, team, channel, last_activity, web_url}``. One
        walk per client instance; clients are built per request.
        """
        if not getattr(self, "_user_token_provided", True):
            return []
        with self._conv_lock:
            if self._conversations is not None:
                return self._conversations
            rows: List[dict] = []
            teams = self._get("/me/joinedTeams?$select=id,displayName").get("value") or []
            for team in teams:
                tid = team.get("id")
                if not tid:
                    continue
                try:
                    chans = self._get(
                        f"/teams/{urllib.parse.quote(tid, safe='')}/channels"
                        "?$select=id,displayName,membershipType,webUrl"
                    ).get("value") or []
                except Exception as exc:  # one unreadable team must not hide the rest
                    logger.warning("teams: channels of team %s unreadable: %s", tid, exc)
                    continue
                for ch in chans:
                    rows.append({
                        "id": f"channel:{tid}/{ch['id']}",
                        "name": f"{team.get('displayName') or 'Team'} › {ch.get('displayName') or 'Channel'}",
                        "kind": "channel",
                        "team": team.get("displayName"),
                        "channel": ch.get("displayName"),
                        "last_activity": None,
                        "web_url": ch.get("webUrl"),
                    })
                    if len(rows) >= _MAX_CONVERSATIONS:
                        break
            if len(rows) < _MAX_CONVERSATIONS:
                me_id = (self._whoami() or {}).get("id")
                url: Optional[str] = (
                    f"{GRAPH_BASE}/me/chats?$expand=members&$top=50"
                    "&$orderby=lastMessagePreview/createdDateTime%20desc"
                )
                pages = 0
                while url and pages < 6 and len(rows) < _MAX_CONVERSATIONS:
                    data = self._get(url)
                    pages += 1
                    for c in data.get("value") or []:
                        rows.append(self._chat_row(c, me_id))
                    url = data.get("@odata.nextLink")
            self._conversations = rows
            return rows

    @staticmethod
    def _chat_row(c: dict, me_id: Optional[str]) -> dict:
        members = c.get("members") or []
        others = [m.get("displayName") for m in members
                  if m.get("displayName") and m.get("userId") != me_id]
        everyone = [m.get("displayName") for m in members if m.get("displayName")]
        kind = "group chat" if c.get("chatType") == "group" else (
            "meeting chat" if c.get("chatType") == "meeting" else "chat")
        if c.get("topic"):
            name = c["topic"]
        elif others:
            name = "Chat with " + ", ".join(others[:4]) + ("…" if len(others) > 4 else "")
        else:
            # Apps and bots are not listed as chat members, so a 1:1 with a bot
            # shows no other member at all.
            name = "Chat with an app"
        return {
            "id": f"chat:{c['id']}",
            "name": name,
            "kind": kind,
            "team": None,
            "channel": None,
            "members": everyone,
            "last_activity": c.get("lastUpdatedDateTime"),
            "web_url": c.get("webUrl"),
        }

    def _conversation_by_id(self, conv_id: str) -> Optional[dict]:
        for row in self.list_conversations():
            if row["id"] == conv_id:
                return row
        return None

    def _conv_name(self, conv_id: str) -> str:
        row = self._conversation_by_id(conv_id)
        if row:
            return row["name"]
        return "Teams channel" if conv_id.startswith("channel:") else "Teams chat"

    def resolve_conversation(self, conversation: str) -> dict:
        """Resolve a conversation id or a name ('General', 'BOW › General',
        'Forecast 2027', 'demo2') to one conversation row."""
        want = str(conversation or "").strip()
        rows = self.list_conversations()
        if want.startswith(("channel:", "chat:")):
            parts = parse_teams_id(want)
            conv_id = (f"channel:{parts['team_id']}/{parts['channel_id']}"
                       if parts["kind"] == "channel" else f"chat:{parts['chat_id']}")
            for row in rows:
                if row["id"] == conv_id:
                    return row
            raise ValueError(f"Conversation {want!r} is not one this user can see.")
        norm = want.lstrip("#").strip().lower()
        exact = [r for r in rows if r["name"].lower() == norm
                 or (r.get("channel") or "").lower() == norm]
        if len(exact) == 1:
            return exact[0]
        loose = exact or [r for r in rows if norm and norm in r["name"].lower()]
        if len(loose) == 1:
            return loose[0]
        names = ", ".join(r["name"] for r in rows[:60]) or "(none)"
        reason = "matches several conversations" if loose else "was not found"
        raise ValueError(
            f"Conversation {conversation!r} {reason}. Conversations this user can see: {names}"
        )

    # ------------------------------------------------------------- messages

    def _sender(self, m: dict) -> str:
        frm = m.get("from") or {}
        who = (frm.get("user") or frm.get("application") or frm.get("device") or {})
        name = who.get("displayName")
        if name:
            return name
        email = (frm.get("emailAddress") or {})
        return email.get("name") or email.get("address") or "Unknown"

    @staticmethod
    def _text(m: dict) -> str:
        body = m.get("body") or {}
        content = body.get("content") or ""
        if (body.get("contentType") or "").lower() == "html":
            content = strip_html(content)
        files = [a.get("name") for a in (m.get("attachments") or [])
                 if a.get("name") and a.get("contentType") == "reference"]
        if files:
            content = (content + "\n" if content else "") + "Attached: " + ", ".join(files)
        return content.strip()

    @staticmethod
    def _is_user_message(m: dict) -> bool:
        return (m.get("messageType") == "message") and not m.get("deletedDateTime")

    def _row(self, m: dict, conv_id: str, conv_name: str, *,
             text: Optional[str] = None, reply_count: Optional[int] = None,
             root_id: Optional[str] = None) -> dict:
        body = text if text is not None else self._text(m)
        sender = self._sender(m)
        snippet = _snippet(body) or "(no text)"
        msg_id = f"{conv_id}/{m.get('id')}"
        if root_id and conv_id.startswith("channel:") and root_id != m.get("id"):
            self._root_cache[msg_id] = root_id
        return {
            "id": msg_id,
            "name": f"{sender}: {snippet}",
            "path": f"{conv_name.translate(_PATH_UNSAFE)}/{sender.translate(_PATH_UNSAFE)}: {snippet.translate(_PATH_UNSAFE)}",
            "mime_type": "text/plain",
            "sender": sender,
            "modified_at": m.get("createdDateTime"),
            "web_url": m.get("webUrl") or m.get("webLink"),
            "preview": " ".join(body.split())[:_PREVIEW_CHARS] or None,
            "folder": conv_name,
            "conversation_id": conv_id,
            "reply_count": reply_count,
            "is_reply": bool(root_id and root_id != m.get("id")) or bool(m.get("replyToId")),
        }

    def _conversation_rows(self, conv: dict, after, before, sender, max_pages: int,
                           cursor_state: Optional[dict] = None) -> Tuple[List[dict], Optional[dict], bool]:
        """Messages of one conversation, newest first, within the window.

        Returns ``(rows, resume_state, exhausted)``. A channel page holds ROOT
        posts with their replies expanded; replies are emitted right after
        their root so a thread reads together.
        """
        conv_id = conv["id"]
        parts = parse_teams_id(conv_id)
        if parts["kind"] == "channel":
            first = (f"{GRAPH_BASE}/teams/{urllib.parse.quote(parts['team_id'], safe='')}"
                     f"/channels/{urllib.parse.quote(parts['channel_id'], safe='')}"
                     f"/messages?$top={_PAGE_SIZE}&$expand=replies")
        else:
            first = (f"{GRAPH_BASE}/chats/{urllib.parse.quote(parts['chat_id'], safe='')}"
                     f"/messages?$top={_PAGE_SIZE}&$orderby=createdDateTime%20desc")
        state = cursor_state or {}
        url = state.get("u") or first
        if not str(url).startswith(GRAPH_BASE + "/"):
            raise ValueError("Invalid cursor — pass back the next_cursor value exactly as returned.")
        rows: List[dict] = []
        pages = 0
        while url and pages < max_pages:
            data = self._get(url)
            pages += 1
            stop = False
            for m in data.get("value") or []:
                if not self._is_user_message(m):
                    continue
                created = _dt(m.get("createdDateTime"))
                replies = [r for r in (m.get("replies") or []) if self._is_user_message(r)]
                # Chats are strictly newest-first, so the first message older
                # than the window ends the walk. A channel root is ordered by
                # its thread's last activity (an old root can carry a reply
                # inside the window), so channels are filtered per message.
                if parts["kind"] == "chat" and after and created and created < after:
                    stop = True
                    break
                thread = [(m, len(replies), m.get("id"))] + [
                    (r, None, m.get("id")) for r in sorted(replies, key=lambda r: r.get("createdDateTime") or "")
                ]
                for msg, rc, root in thread:
                    when = _dt(msg.get("createdDateTime"))
                    if after and when and when < after:
                        continue
                    if before and when and when >= before:
                        continue
                    row = self._row(msg, conv_id, conv["name"], reply_count=rc, root_id=root)
                    if sender and sender.lower() not in (row["sender"] or "").lower():
                        continue
                    rows.append(row)
            url = None if stop else data.get("@odata.nextLink")
        exhausted = not url
        return rows, ({"u": url} if url else None), exhausted

    def list_messages(
        self,
        conversation: Optional[str] = None,
        from_user: Optional[str] = None,
        sent_after: Optional[str] = None,
        sent_before: Optional[str] = None,
        max_results: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Messages newest first. With a conversation, paged to completion;
        without one, the newest messages across every conversation.

        Returns ``{items, next_cursor, folder, partial}``.
        """
        if not getattr(self, "_user_token_provided", True):
            return {"items": [], "next_cursor": None, "folder": None}
        n = clamp_mail_results(max_results)
        after = parse_mail_datetime(sent_after)
        before = parse_mail_datetime(sent_before)
        state = decode_cursor(cursor)

        if conversation:
            conv = self.resolve_conversation(conversation)
            skip = int(state.get("o") or 0)
            rows, resume, exhausted = self._conversation_rows(
                conv, after, before, from_user, _MAX_PAGES, {"u": state["u"]} if state.get("u") else None)
            # The cursor carries the Graph page to resume from plus an offset
            # into the rows already seen from it.
            page = rows[skip: skip + n]
            if len(rows) > skip + n:
                nxt = encode_cursor({**({"u": state["u"]} if state.get("u") else {}), "o": skip + n})
            elif resume:
                nxt = encode_cursor(resume)
            else:
                nxt = None
            return {"items": page, "next_cursor": nxt, "folder": self._folder(conv)}

        # No conversation: fan out over every conversation (bounded) and merge.
        convs = self.list_conversations()
        if not convs:
            return {"items": [], "next_cursor": None, "folder": None}

        def one(conv):
            try:
                return self._conversation_rows(conv, after, before, from_user, _FANOUT_PAGES)
            except Exception as exc:
                logger.warning("teams: listing %s failed: %s", conv["id"], exc)
                return [], None, True

        with ThreadPoolExecutor(max_workers=_FANOUT_WORKERS) as pool:
            results = list(pool.map(one, convs))
        merged: List[dict] = []
        partial: List[str] = []
        for conv, (rows, _resume, exhausted) in zip(convs, results):
            merged.extend(rows)
            if not exhausted:
                partial.append(conv["name"])
        merged.sort(key=lambda r: r.get("modified_at") or "", reverse=True)
        skip = int(state.get("o") or 0)
        page = merged[skip: skip + n]
        nxt = encode_cursor({"o": skip + n}) if len(merged) > skip + n else None
        return {"items": page, "next_cursor": nxt, "folder": None, "partial": partial}

    @staticmethod
    def _folder(conv: dict) -> dict:
        return {"id": conv["id"], "name": conv["name"], "path": conv["name"], "kind": conv.get("kind")}

    # --------------------------------------------------------------- search

    def search_messages(
        self,
        query: str,
        conversation: Optional[str] = None,
        from_user: Optional[str] = None,
        sent_after: Optional[str] = None,
        sent_before: Optional[str] = None,
        max_results: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Graph Search over every channel and chat message the user can see.

        Microsoft Search enforces the user's own access: a chat they are not in
        never comes back. Conversation / sender / window filters are applied to
        the returned hits.
        """
        if not getattr(self, "_user_token_provided", True):
            return {"items": [], "next_cursor": None, "folder": None}
        q = (query or "").strip()
        if not q:
            raise ValueError("search_chat_messages needs a non-empty query; use list_chat_messages to browse.")
        n = clamp_mail_results(max_results)
        after = parse_mail_datetime(sent_after)
        before = parse_mail_datetime(sent_before)
        conv = self.resolve_conversation(conversation) if conversation else None
        state = decode_cursor(cursor)
        if cursor:
            # The cursor carries the query that produced it, so a broadened
            # search keeps paging the broadened results.
            q = state.get("q") or q
            return self._search_walk(q, conv, after, before, from_user, n, state, bool(state.get("b")))
        result = self._search_walk(q, conv, after, before, from_user, n, {}, False)
        if not result["items"]:
            wider = broadened_query(q)
            if wider:
                result = self._search_walk(wider, conv, after, before, from_user, n, {}, True)
        return result

    def _search_walk(self, q: str, conv: Optional[dict], after, before, from_user: Optional[str],
                     n: int, state: dict, broadened: bool) -> Dict[str, Any]:
        offset = int(state.get("f") or 0)
        skip_in_page = int(state.get("o") or 0)
        items: List[dict] = []
        folder = self._folder(conv) if conv else None

        def done(nxt: Optional[dict]) -> Dict[str, Any]:
            cur = encode_cursor({**nxt, "q": q, "b": 1 if broadened else 0}) if nxt else None
            out = {"items": items, "next_cursor": cur, "folder": folder}
            if broadened:
                out["broadened_query"] = q
            return out

        for _ in range(_MAX_SEARCH_PAGES):
            resp = self._search_page(q, offset)
            hits = resp["hits"]
            for i in range(skip_in_page, len(hits)):
                row = self._hit_row(hits[i])
                if row is None:
                    continue
                if conv and row["conversation_id"] != conv["id"]:
                    continue
                when = _dt(row.get("modified_at"))
                if after and when and when < after:
                    continue
                if before and when and when >= before:
                    continue
                if from_user and from_user.lower() not in (row["sender"] or "").lower():
                    continue
                if len(items) >= n:
                    return done({"f": offset, "o": i})
                items.append(row)
            skip_in_page = 0
            if not resp["more"] or not hits:
                return done(None)
            offset += len(hits)
        return done({"f": offset, "o": 0})

    def _search_page(self, query: str, offset: int) -> Dict[str, Any]:
        payload = {"requests": [{
            "entityTypes": ["chatMessage"],
            "query": {"queryString": query},
            "from": offset,
            "size": _SEARCH_PAGE,
        }]}
        # Through _request so search shares the app-wide throttle discipline.
        resp = self._request(
            "POST", "/search/query", json_body=payload,
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code >= 400:
            raise ValueError(f"Graph search → {resp.status_code} {resp.text[:300]}")
        data = resp.json()
        hits: List[dict] = []
        more = False
        for block in data.get("value") or []:
            for container in block.get("hitsContainers") or []:
                hits.extend(container.get("hits") or [])
                more = more or bool(container.get("moreResultsAvailable"))
        return {"hits": hits, "more": more}

    def _channel_threads(self, team_id: str, channel_id: str, max_pages: int) -> Dict[str, dict]:
        """Every message id seen in the channel's newest ``max_pages`` pages of
        root posts → its thread root (replies expanded). Cached per client."""
        key = (team_id, channel_id)
        idx = self._thread_index.get(key)
        if idx is not None and idx.get("__pages__", 0) >= max_pages:
            return idx
        idx: Dict[str, Any] = {}
        url: Optional[str] = (f"{GRAPH_BASE}{self._channel_base(team_id, channel_id)}"
                              f"?$top={_PAGE_SIZE}&$expand=replies")
        pages = 0
        while url and pages < max_pages:
            data = self._get(url)
            pages += 1
            for root in data.get("value") or []:
                if not root.get("id"):
                    continue
                idx[root["id"]] = root
                for rep in root.get("replies") or []:
                    if rep.get("id"):
                        idx[rep["id"]] = root
            url = data.get("@odata.nextLink")
        # "__pages__" records how deep this index goes; a fully walked channel
        # satisfies any later request.
        idx["__pages__"] = max_pages if url else 10**6
        self._thread_index[key] = idx
        return idx

    def _context_line(self, m: dict, arrow: str) -> str:
        return f"{arrow} {self._sender(m)}: {_snippet(self._text(m), _CONTEXT_CHARS)}"

    def _annotate_thread(self, row: dict, team_id: str, channel_id: str, message_id: str) -> None:
        """Give a channel hit its thread: the post it answers, the replies next
        to it, and how many replies the discussion has. A hit is one line of a
        conversation; the reason, the numbers and the final decision are
        usually elsewhere in it."""
        try:
            root = self._channel_threads(team_id, channel_id, _SEARCH_THREAD_PAGES).get(message_id)
        except Exception as exc:  # context is a bonus; never fail the search
            logger.info("teams: thread context unavailable for %s: %s", channel_id, exc)
            return
        if not root:
            return
        replies = sorted((r for r in (root.get("replies") or []) if self._is_user_message(r)),
                         key=lambda r: r.get("createdDateTime") or "")
        row["reply_count"] = len(replies)
        if root.get("id") == message_id:
            row["context"] = [self._context_line(r, "↓") for r in replies[:_CONTEXT_AFTER]]
            row["more_after"] = max(0, len(replies) - _CONTEXT_AFTER)
            return
        row["is_reply"] = True
        self._root_cache[row["id"]] = root["id"]
        row["thread"] = f"{self._sender(root)}: {_snippet(self._text(root), 160)}"
        pos = next((i for i, r in enumerate(replies) if r.get("id") == message_id), None)
        if pos is not None:
            row["context"] = (
                [self._context_line(r, "↑") for r in replies[max(0, pos - _CONTEXT_BEFORE):pos]]
                + [self._context_line(r, "↓") for r in replies[pos + 1: pos + 1 + _CONTEXT_AFTER]]
            )
            row["more_after"] = max(0, len(replies) - (pos + 1 + _CONTEXT_AFTER))

    def _annotate_chat(self, row: dict, chat_id: str, message_id: str) -> None:
        """Give a chat hit the messages right before and after it — a question
        and its answer rarely share the searched words."""
        recent = self._chat_recent.get(chat_id)
        if recent is None:
            recent = []
            try:
                url: Optional[str] = (f"{GRAPH_BASE}/chats/{urllib.parse.quote(chat_id, safe='')}"
                                      f"/messages?$top={_PAGE_SIZE}&$orderby=createdDateTime%20desc")
                for _ in range(_SEARCH_THREAD_PAGES):
                    if not url:
                        break
                    data = self._get(url)
                    recent.extend(m for m in (data.get("value") or []) if self._is_user_message(m))
                    url = data.get("@odata.nextLink")
            except Exception as exc:  # context is a bonus; never fail the search
                logger.info("teams: chat context unavailable for %s: %s", chat_id, exc)
            recent.reverse()
            self._chat_recent[chat_id] = recent
        pos = next((i for i, m in enumerate(recent) if m.get("id") == message_id), None)
        if pos is None:
            return
        row["context"] = (
            [self._context_line(m, "↑") for m in recent[max(0, pos - _CONTEXT_BEFORE):pos]]
            + [self._context_line(m, "↓") for m in recent[pos + 1: pos + 1 + _CONTEXT_AFTER]]
        )
        row["more_after"] = max(0, len(recent) - (pos + 1 + _CONTEXT_AFTER))

    def _hit_row(self, hit: dict) -> Optional[dict]:
        r = hit.get("resource") or {}
        msg_id = r.get("id")
        if not msg_id:
            return None
        ident = r.get("channelIdentity") or {}
        if ident.get("teamId") and ident.get("channelId"):
            conv_id = f"channel:{ident['teamId']}/{ident['channelId']}"
        elif r.get("chatId"):
            conv_id = f"chat:{r['chatId']}"
        else:
            return None
        summary = _HIGHLIGHT_RE.sub("", hit.get("summary") or "")
        summary = strip_html(summary) if "<" in summary else summary
        fake = {
            "id": msg_id,
            "createdDateTime": r.get("createdDateTime"),
            "webUrl": r.get("webLink"),
            "from": r.get("from") or {},
        }
        row = self._row(fake, conv_id, self._conv_name(conv_id), text=summary)
        if ident.get("teamId") and ident.get("channelId"):
            self._annotate_thread(row, ident["teamId"], ident["channelId"], msg_id)
        else:
            self._annotate_chat(row, r["chatId"], msg_id)
        return row

    # ----------------------------------------------------------------- read

    def _channel_base(self, team_id: str, channel_id: str) -> str:
        return (f"/teams/{urllib.parse.quote(team_id, safe='')}"
                f"/channels/{urllib.parse.quote(channel_id, safe='')}/messages")

    def _find_root(self, team_id: str, channel_id: str, message_id: str) -> str:
        """The thread root of a channel message (itself when it is a root)."""
        key = f"channel:{team_id}/{channel_id}/{message_id}"
        if key in self._root_cache:
            return self._root_cache[key]
        base = self._channel_base(team_id, channel_id)
        resp = self._request("GET", f"{base}/{urllib.parse.quote(message_id, safe='')}")
        if resp.status_code < 400:
            return message_id
        if resp.status_code not in (400, 404):
            raise ValueError(f"Graph {resp.url} → {resp.status_code} {resp.text[:300]}")
        # A reply: Graph will not serve it by id outside its thread, and search
        # hits do not name the parent. Scan root posts (replies expanded),
        # newest activity first, up to a page cap.
        root = self._channel_threads(team_id, channel_id, _MAX_ROOT_SCAN_PAGES).get(message_id)
        if root:
            self._root_cache[key] = root["id"]
            return root["id"]
        raise ValueError(
            "That channel message could not be found — it may have been deleted, or "
            "it is in a thread older than this connection scans. Search again and read "
            "a newer hit."
        )

    def _read_channel_thread(self, team_id: str, channel_id: str, message_id: Optional[str]) -> str:
        conv_id = f"channel:{team_id}/{channel_id}"
        conv_name = self._conv_name(conv_id)
        base = self._channel_base(team_id, channel_id)
        if not message_id:
            return self._read_recent(self._conversation_by_id(conv_id) or {"id": conv_id, "name": conv_name})
        root_id = self._find_root(team_id, channel_id, message_id)
        root = self._get(f"{base}/{urllib.parse.quote(root_id, safe='')}")
        replies: List[dict] = []
        url: Optional[str] = f"{GRAPH_BASE}{base}/{urllib.parse.quote(root_id, safe='')}/replies?$top={_PAGE_SIZE}"
        pages = 0
        while url and pages < _MAX_PAGES:
            data = self._get(url)
            replies.extend(r for r in (data.get("value") or []) if self._is_user_message(r))
            url = data.get("@odata.nextLink")
            pages += 1
        replies.sort(key=lambda r: r.get("createdDateTime") or "")
        lines = [
            f"Teams channel: {conv_name}",
            f"Thread started by {self._sender(root)} on {_fmt_time(root.get('createdDateTime'))}"
            f" · {len(replies)} repl{'y' if len(replies) == 1 else 'ies'}",
        ]
        link = root.get("webUrl")
        if link:
            lines.append(f"Link: {link}")
        lines.append("")
        for msg in [root] + replies:
            marker = "▶ " if msg.get("id") == message_id and message_id != root_id else ""
            indent = "" if msg is root else "    ↳ "
            lines.append(f"{indent}{marker}[{_fmt_time(msg.get('createdDateTime'))}] "
                         f"{self._sender(msg)}: {self._text(msg) or '(no text)'}")
        return "\n".join(lines)

    def _read_chat_window(self, chat_id: str, message_id: Optional[str]) -> str:
        conv_id = f"chat:{chat_id}"
        conv = self._conversation_by_id(conv_id) or {"id": conv_id, "name": "Teams chat"}
        if not message_id:
            return self._read_recent(conv)
        url: Optional[str] = (f"{GRAPH_BASE}/chats/{urllib.parse.quote(chat_id, safe='')}"
                              f"/messages?$top={_PAGE_SIZE}&$orderby=createdDateTime%20desc")
        newest_first: List[dict] = []
        found_at: Optional[int] = None
        pages = 0
        while url and pages < _MAX_PAGES:
            data = self._get(url)
            pages += 1
            for m in data.get("value") or []:
                if not self._is_user_message(m):
                    continue
                newest_first.append(m)
                if m.get("id") == message_id:
                    found_at = len(newest_first) - 1
            if found_at is not None and len(newest_first) - found_at > _CHAT_CONTEXT:
                break
            url = data.get("@odata.nextLink")
        if found_at is None:
            raise ValueError("That chat message could not be found — it may have been deleted.")
        lo = max(0, found_at - _CHAT_CONTEXT)
        window = list(reversed(newest_first[lo: found_at + _CHAT_CONTEXT + 1]))
        target = newest_first[found_at]
        lines = [f"Teams {conv.get('kind') or 'chat'}: {conv['name']}"]
        if conv.get("members"):
            lines.append("Participants: " + ", ".join(conv["members"]))
        link = target.get("webUrl") or conv.get("web_url")
        if link:
            lines.append(f"Link: {link}")
        lines.append(f"Showing {len(window)} message(s) around the requested one (▶).")
        lines.append("")
        for m in window:
            marker = "▶ " if m.get("id") == message_id else ""
            lines.append(f"{marker}[{_fmt_time(m.get('createdDateTime'))}] {self._sender(m)}: "
                         f"{self._text(m) or '(no text)'}")
        return "\n".join(lines)

    def _read_recent(self, conv: dict, limit: int = 50) -> str:
        rows, _resume, _exhausted = self._conversation_rows(conv, None, None, None, 2)
        rows = rows[:limit]
        lines = [f"Teams {conv.get('kind') or 'conversation'}: {conv['name']}",
                 f"Newest {len(rows)} message(s), newest first.", ""]
        for r in rows:
            indent = "    ↳ " if r.get("is_reply") else ""
            lines.append(f"{indent}[{_fmt_time(r.get('modified_at'))}] {r['sender']}: {r.get('preview') or ''}")
        return "\n".join(lines)

    def read_file(self, file_id: str, **_) -> Any:
        parts = parse_teams_id(file_id)
        if parts["kind"] == "channel":
            return self._read_channel_thread(parts["team_id"], parts["channel_id"], parts["message_id"])
        return self._read_chat_window(parts["chat_id"], parts["message_id"])

    # ----------------------------------------------- file-shaped compatibility

    # A message has no original bytes to preview. The browse route offers a
    # preview only when the client has `read_raw_bytes`; the drive reader
    # inherited from GraphDriveClient would request /drives/... and fail, so
    # hide it (hasattr is False when the attribute raises AttributeError).
    @property
    def read_raw_bytes(self):
        raise AttributeError("Teams messages have no original bytes to preview")

    def list_files(self, folder_id: Optional[str] = None, recursive: Optional[bool] = None) -> List[dict]:
        """The browse view: newest messages, each under its conversation so the
        file browser renders one folder per channel / chat."""
        return self.list_messages(conversation=folder_id, max_results=200)["items"]

    def search_files(self, query: str, max_results: Optional[int] = None, **_) -> List[dict]:
        return self.search_messages(query, max_results=max_results)["items"]

    # Teams messages have no pre-indexed catalog — searched / read live per user.
    @discovery_progress
    def get_schemas(self, *args, progress_callback=None, **kwargs) -> List:
        return []

    def test_connection(self) -> dict:
        # Admin-only (service-principal credentials, no user token yet): every
        # endpoint here is `/me/*`, which Graph serves for delegated tokens only.
        # Verify the credentials mint a token and stop — same as Outlook.
        if not getattr(self, "_user_token_provided", True):
            try:
                self._token()
            except Exception as e:
                return {"success": False, "message": str(e)}
            return {
                "success": True,
                "message": (
                    "Service principal credentials verified. Each user signs in "
                    "with Microsoft to search their own Teams channels and chats."
                ),
            }
        try:
            me = self._whoami()
            who = me.get("userPrincipalName") or me.get("displayName") or "Microsoft account"
        except Exception as e:
            return {"success": False, "message": str(e)}
        try:
            teams = self._get("/me/joinedTeams?$select=id").get("value") or []
        except Exception as e:
            detail = str(e)
            if "403" in detail or "Forbidden" in detail:
                return {"success": False, "message": (
                    f"Signed in as {who}, but the app registration lacks the Teams "
                    "permissions. Grant the delegated Microsoft Graph permissions "
                    "Team.ReadBasic.All, Channel.ReadBasic.All, ChannelMessage.Read.All "
                    "and Chat.Read, with admin consent."
                )}
            return {"success": False, "message": f"Signed in as {who}, but Teams is unreadable: {detail}"}
        return {"success": True, "message": f"Connected as {who} · {len(teams)} team(s)"}

"""Slack messages over the Slack Web API, read live with the user's token.

The Slack sibling of the Microsoft Teams client (``graph_teams_client``):
nothing is indexed or stored, every call is a live Web API request made AS the
signed-in user (a ``xoxp-`` user token from "Sign in with Slack"), so a user
only ever sees the channels and DMs they are a member of.

The agent surface is the same chat vocabulary Teams uses:

  search_chat_messages -> ``search.messages`` (user tokens only) across every
                          conversation the user can see
  list_chat_messages   -> ``conversations.history`` of one conversation (or
                          the newest across all of them); with
                          ``list_conversations=true`` the conversations
                          themselves (``users.conversations``)
  read_chat_thread     -> ``conversations.replies``: the whole thread around a
                          message, or the window around an unthreaded one

Per-agent scope (``allowed_channels`` + ``include_direct_messages``, passed
in by the caller from the agent's "Channels" choice — see
``app.services.slack_channel_scope``): the channel list narrows channels and
the DM switch decides DMs, independently. It only ever NARROWS what the
user's own token can see.

Ids are readable and self-describing:

  conversation: ``slack:<channelId>``
  message:      ``slack:<channelId>/<ts>``

Slack channel ids never contain ``/`` and a ``ts`` is ``<seconds>.<micros>``,
so the split is unambiguous.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import httpx

from app.data_sources.clients.base import Capability, DataSourceClient
from app.data_sources.clients.mail_common import (
    clamp_mail_results,
    decode_cursor,
    encode_cursor,
    parse_mail_datetime,
)
from app.data_sources.clients.progress import discovery_progress

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"

# Pages are requested at this size from conversations.* (Slack recommends ≤200).
_PAGE_SIZE = 200
# search.messages serves at most 100 matches per page and 100 pages.
_SEARCH_PAGE = 100
_MAX_SEARCH_PAGES = 10
# Safety valves on one call's page walk.
_MAX_PAGES = 10
_MAX_CONVERSATION_PAGES = 10
# Fan-out listing (no conversation named): conversations scanned and workers.
# conversations.history is Tier 3 (~50/min per workspace per app), so stay low.
_FANOUT_CONVERSATIONS = 20
_FANOUT_WORKERS = 4
_FANOUT_PAGE = 50
# Parallel users.info lookups when naming DM partners (Tier 4: 100+/min).
_USER_WORKERS = 8
# Threads whose replies one client instance (one tool call) fetches when
# listing — conversations.replies is Tier 3 (~50/min per workspace per app).
_MAX_THREAD_EXPANSIONS = 30
# ...of which a fan-out over many conversations may spend at most this many,
# so one call stays well under Tier 3 (≈20 histories + 10 threads).
_FANOUT_THREAD_EXPANSIONS = 10
# Search hits: threads fetched for hit context, and the neighbouring replies
# shown with each hit (same shape as Teams).
_SEARCH_CONTEXT_THREADS = 10
_CONTEXT_BEFORE = 2
_CONTEXT_AFTER = 3
_CONTEXT_CHARS = 200
# Broadened retry: at most this many single-word searches (search.messages is
# Tier 2, ~20/min).
_MAX_BROADENED_TERMS = 3
# Most allowed channels named as in:#x filters in one search query.
_MAX_IN_FILTERS = 10
# Messages shown each side of an unthreaded message on read.
_WINDOW = 10
_PREVIEW_CHARS = 255
# 429 handling: retries per request and the longest single wait honoured.
_MAX_RETRIES = 3
_MAX_RETRY_WAIT = 30.0

#: User-token scopes the connector needs (requested at sign-in).
REQUIRED_SCOPES = (
    "channels:read", "groups:read", "channels:history", "groups:history",
    "search:read", "users:read",
)
DM_SCOPES = ("im:read", "im:history", "mpim:read", "mpim:history")

# Slack error codes → what the user should do about them.
_AUTH_ERRORS = {"invalid_auth", "not_authed", "token_revoked", "token_expired",
                "account_inactive", "no_permission"}
_FRIENDLY = {
    "not_in_channel": "You are not a member of that Slack channel, so it can't be read.",
    "channel_not_found": "That Slack conversation was not found, or you don't have access to it.",
    "thread_not_found": "That Slack thread was not found — it may have been deleted.",
    "missing_scope": ("The Slack app is missing a permission this needs. Ask an admin to add the "
                      "user token scopes listed in the connection setup, then sign in again."),
    "ratelimited": "Slack is rate-limiting requests right now. Try again in a minute.",
}

_PATH_UNSAFE = str.maketrans({"/": "∕", "\n": " ", "\r": " "})
_USER_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]*))?>")
_CHANNEL_MENTION_RE = re.compile(r"<#([CGD][A-Z0-9]+)(?:\|([^>]*))?>")
_SPECIAL_MENTION_RE = re.compile(r"<!(here|channel|everyone)(?:\|[^>]*)?>")
_SUBTEAM_RE = re.compile(r"<!subteam\^[A-Z0-9]+(?:\|([^>]*))?>")
_LINK_RE = re.compile(r"<((?:https?|mailto):[^>|]+)(?:\|([^>]*))?>")
_CHANNEL_ID_RE = re.compile(r"^[CGD][A-Z0-9]{6,}$")
_THREAD_TS_RE = re.compile(r"[?&]thread_ts=([0-9]+\.[0-9]+)")

# Message subtypes that are not something a person said.
_SKIP_SUBTYPES = frozenset({
    "channel_join", "channel_leave", "group_join", "group_leave", "channel_topic",
    "channel_purpose", "channel_name", "group_topic", "group_purpose", "group_name",
    "channel_archive", "channel_unarchive", "group_archive", "group_unarchive",
    "pinned_item", "unpinned_item", "tombstone", "joiner_notification_for_inviter",
})


class SlackApiError(ValueError):
    """A Slack Web API call answered ``ok: false`` (or HTTP ≥ 400)."""

    def __init__(self, method: str, error: str, needed: Optional[str] = None):
        self.method = method
        self.error = error
        self.needed = needed
        if error in _AUTH_ERRORS:
            msg = "Your Slack sign-in is no longer valid. Sign in with Slack again to reconnect."
        elif error == "missing_scope" and needed:
            msg = _FRIENDLY["missing_scope"] + f" (missing: {needed})"
        else:
            msg = _FRIENDLY.get(error) or f"Slack {method} failed: {error}"
        super().__init__(msg)


def ts_to_iso(ts: Optional[str]) -> Optional[str]:
    try:
        moment = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _to_ts(moment: Optional[datetime]) -> Optional[str]:
    return None if moment is None else f"{moment.timestamp():.6f}"


def _fmt_time(ts: Optional[str]) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OverflowError):
        return str(ts or "")


def _snippet(text: str, n: int = 60) -> str:
    one = " ".join((text or "").split())
    return one if len(one) <= n else one[: n - 1] + "…"


def parse_slack_id(value: str) -> Dict[str, Optional[str]]:
    """``slack:<channel>[/<ts>]`` → ``{channel_id, ts}``; ValueError otherwise."""
    raw = str(value or "").strip()
    kind, _, rest = raw.partition(":")
    channel, _, ts = rest.partition("/")
    if kind == "slack" and _CHANNEL_ID_RE.match(channel or "") and (not ts or re.match(r"^\d+\.\d+$", ts)):
        return {"channel_id": channel, "ts": ts or None}
    raise ValueError(
        f"{raw!r} is not a Slack conversation or message id. Pass an id exactly as "
        "returned by search_chat_messages / list_chat_messages (e.g. 'slack:C0123ABCD/1700000000.000100')."
    )


def parse_allowed_channels(value: Any) -> List[str]:
    """``"#support, escalations C0123ABCD"`` → ``["support", "escalations", "C0123ABCD"]``.

    Names are matched case-insensitively without the leading ``#``; ids keep
    their case. Accepts a list too (schema forms may send either).
    """
    if not value:
        return []
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,\s]+", str(value))
    out: List[str] = []
    for item in items:
        token = str(item or "").strip().lstrip("#").strip()
        if not token:
            continue
        token = token if _CHANNEL_ID_RE.match(token) else token.lower()
        if token not in out:
            out.append(token)
    return out


def slack_broadened_terms(query: str) -> Optional[List[str]]:
    """The distinctive words of a plain multi-word query, longest first, for
    the one-word-at-a-time retry. None for one word, or when the caller wrote
    Slack search syntax (quotes, ``in:``, ``from:``…). Same stopwords as Teams."""
    from app.data_sources.clients.graph_teams_client import broadened_query

    if re.search(r"\b[a-z]+:", query or ""):
        return None
    wider = broadened_query(query)
    if not wider:
        return None
    words = [w for w in wider.split(" OR ") if w not in _HE_STOPWORDS]
    if len(words) < 2:
        return words or None
    return sorted(words, key=len, reverse=True)[:_MAX_BROADENED_TERMS]


# Hebrew question / function words that never decide a match (the shared
# stopword list is English-only).
_HE_STOPWORDS = frozenset("למה מה איך מי מתי איפה האם של על את עם זה זו כל יש אין גם או".split())


def format_allowed_channels(allowed: List[str]) -> str:
    """``["support", "C0123ABCD"]`` → ``"#support, C0123ABCD"``."""
    return ", ".join(a if _CHANNEL_ID_RE.match(a) else f"#{a}" for a in allowed)


class SlackMessagesClient(DataSourceClient):
    """Slack channels and DMs, searched and read live as the signed-in user."""

    capabilities = {
        Capability.LIST_CHAT_MESSAGES,
        Capability.READ_CHAT_THREAD,
        Capability.SEARCH_CHAT_MESSAGES,
    }

    def __init__(
        self,
        access_token: Optional[str] = None,
        include_direct_messages: Optional[bool] = True,
        allowed_channels: Any = None,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Optional[Callable[[float], None]] = None,
        **_ignored,
    ):
        super().__init__()
        self.access_token = (access_token or "").strip() or None
        self.allowed = parse_allowed_channels(allowed_channels)
        # Channels and DMs are scoped independently: the channel list narrows
        # channels only, and DMs follow their own switch (an agent can read
        # #support plus the user's DMs, or #support alone).
        self.include_dms = bool(include_direct_messages is None or include_direct_messages)
        # Test-only injection points for deterministic external-boundary tests.
        self._transport = transport
        self._sleep = sleep or time.sleep
        self._http: Optional[httpx.Client] = None
        self._http_lock = threading.Lock()
        self._conv_lock = threading.Lock()
        self._users_lock = threading.Lock()
        self._auth: Optional[dict] = None
        self._conversations: Optional[List[dict]] = None
        self._users: Dict[str, str] = {}
        self._last_scopes: Optional[Set[str]] = None
        self._expansions_left = _MAX_THREAD_EXPANSIONS
        self._expansions_lock = threading.Lock()

    @property
    def description(self) -> str:
        scope = f" (channels: {format_allowed_channels(self.allowed)})" if self.allowed else ""
        return "Slack channels" + (" and DMs" if self.include_dms else "") + \
            " the signed-in user can see" + scope

    @property
    def is_document_based(self) -> bool:
        return True

    # ----------------------------------------------------------------- http

    def _client(self) -> httpx.Client:
        with self._http_lock:
            if self._http is None:
                self._http = httpx.Client(timeout=30, transport=self._transport)
            return self._http

    def _call(self, method: str, **params: Any) -> dict:
        """GET ``https://slack.com/api/<method>`` with the user token.

        Arguments go in the query string only — Slack rejects GET requests
        that carry a body (from 2027-02-17). Slack reports failures as
        HTTP 200 + ``ok: false``; both that and HTTP ≥ 400 raise
        :class:`SlackApiError`. HTTP 429 waits ``Retry-After`` and retries.
        """
        if not self.access_token:
            raise SlackApiError(method, "not_authed")
        query = {k: v for k, v in params.items() if v is not None and v != ""}
        headers = {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}
        attempt = 0
        while True:
            resp = self._client().get(f"{SLACK_API}/{method}", params=query, headers=headers)
            if resp.status_code == 429 and attempt < _MAX_RETRIES:
                attempt += 1
                try:
                    wait = float(resp.headers.get("Retry-After") or 1)
                except ValueError:
                    wait = 1.0
                logger.info("slack: %s rate-limited, retrying in %.1fs", method, wait)
                self._sleep(min(max(wait, 0.0), _MAX_RETRY_WAIT))
                continue
            break
        if resp.status_code == 429:
            raise SlackApiError(method, "ratelimited")
        scopes = resp.headers.get("x-oauth-scopes")
        if scopes is not None:
            self._last_scopes = {s.strip() for s in scopes.split(",") if s.strip()}
        try:
            data = resp.json()
        except Exception:
            raise SlackApiError(method, f"HTTP {resp.status_code}")
        if resp.status_code >= 400 or not data.get("ok"):
            raise SlackApiError(method, str(data.get("error") or f"HTTP {resp.status_code}"), data.get("needed"))
        return data

    # ------------------------------------------------------------- identity

    def _whoami(self) -> dict:
        if self._auth is None:
            self._auth = self._call("auth.test")
        return self._auth

    def _workspace_url(self) -> str:
        url = str((self._whoami() or {}).get("url") or "https://slack.com/")
        return url if url.endswith("/") else url + "/"

    def permalink(self, channel_id: str, ts: str, thread_ts: Optional[str] = None) -> str:
        """The message's Slack link, built locally (no chat.getPermalink call)."""
        link = f"{self._workspace_url()}archives/{channel_id}/p{ts.replace('.', '')}"
        if thread_ts and thread_ts != ts:
            link += f"?thread_ts={thread_ts}&cid={channel_id}"
        return link

    def _user_name(self, user_id: Optional[str]) -> Optional[str]:
        if not user_id:
            return None
        with self._users_lock:
            if user_id in self._users:
                return self._users[user_id]
        name = None
        try:
            user = self._call("users.info", user=user_id).get("user") or {}
            prof = user.get("profile") or {}
            name = (prof.get("display_name") or prof.get("real_name")
                    or user.get("real_name") or user.get("name"))
        except SlackApiError as exc:
            logger.info("slack: users.info %s unavailable: %s", user_id, exc.error)
        with self._users_lock:
            self._users[user_id] = name or user_id
        return self._users[user_id]

    def _prefetch_users(self, user_ids) -> None:
        with self._users_lock:
            todo = sorted({u for u in user_ids if u and u not in self._users})
        if len(todo) > 1:
            with ThreadPoolExecutor(max_workers=_USER_WORKERS) as pool:
                list(pool.map(self._user_name, todo))

    # -------------------------------------------------------- conversations

    def _types(self) -> str:
        return "public_channel,private_channel" + (",im,mpim" if self.include_dms else "")

    def _is_allowed(self, ch: dict) -> bool:
        if ch.get("is_im") or ch.get("is_mpim"):
            return self.include_dms
        if not self.allowed:
            return True
        return ch.get("id") in self.allowed or str(ch.get("name") or "").lower() in self.allowed

    def _conv_row(self, ch: dict) -> dict:
        cid = ch["id"]
        if ch.get("is_im"):
            other = self._user_name(ch.get("user")) or "someone"
            name, kind = f"DM with {other}", "direct message"
        elif ch.get("is_mpim"):
            # mpdm-alice--bob--carol-1 → Group DM: alice, bob, carol
            handles = re.sub(r"^mpdm-|-\d+$", "", str(ch.get("name") or "")).split("--")
            name, kind = "Group DM: " + ", ".join(h for h in handles if h), "group DM"
        else:
            name = f"#{ch.get('name') or cid}"
            kind = "private channel" if ch.get("is_private") else "channel"
        return {
            "id": f"slack:{cid}",
            "channel_id": cid,
            "name": name,
            "channel": ch.get("name"),
            "kind": kind,
            "last_activity": ts_to_iso(ch.get("updated") / 1000) if isinstance(ch.get("updated"), (int, float)) else None,
            "web_url": f"{self._workspace_url()}archives/{cid}",
        }

    def list_conversations(self) -> List[dict]:
        """Every conversation the user is a member of (``users.conversations``
        returns memberships only — never every public channel), narrowed by
        the agent's channel scope. Rows: ``{id, name, kind, channel,
        last_activity, web_url}``. One walk per client instance."""
        if not self.access_token:
            return []
        with self._conv_lock:
            if self._conversations is not None:
                return self._conversations
            rows: List[dict] = []
            cursor: Optional[str] = None
            for _ in range(_MAX_CONVERSATION_PAGES):
                data = self._call("users.conversations", types=self._types(),
                                  exclude_archived="true", limit=_PAGE_SIZE, cursor=cursor)
                # Name every DM partner on the page in parallel (users.info is
                # Tier 4) — one at a time this took seconds for a busy user.
                self._prefetch_users(ch.get("user") for ch in data.get("channels") or [] if ch.get("is_im"))
                for ch in data.get("channels") or []:
                    if ch.get("id") and self._is_allowed(ch):
                        rows.append(self._conv_row(ch))
                cursor = (data.get("response_metadata") or {}).get("next_cursor") or None
                if not cursor:
                    break
            self._conversations = rows
            return rows

    def _conversation_by_channel(self, channel_id: str) -> Optional[dict]:
        for row in self.list_conversations():
            if row["channel_id"] == channel_id:
                return row
        return None

    def _require_conversation(self, channel_id: str) -> dict:
        row = self._conversation_by_channel(channel_id)
        if row:
            return row
        if self.allowed:
            raise ValueError("That Slack conversation is not one of the channels this agent reads.")
        raise ValueError("That Slack conversation is not one this user is a member of.")

    def _conversation_named(self, text: str) -> Optional[dict]:
        """The visible conversation whose name IS ``text`` (``#x`` or ``x``), if any."""
        norm = str(text or "").strip().lstrip("#").strip().lower()
        if not norm:
            return None
        for row in self.list_conversations():
            if (row.get("channel") or "").lower() == norm or row["name"].lower().lstrip("#") == norm:
                return row
        return None

    def resolve_conversation(self, conversation: str) -> dict:
        """A conversation id (``slack:C…`` or bare ``C…``) or a name
        (``#support``, ``support``, ``DM with Dana``) → one visible row."""
        want = str(conversation or "").strip()
        if want.startswith("slack:"):
            return self._require_conversation(parse_slack_id(want)["channel_id"])
        if _CHANNEL_ID_RE.match(want):
            return self._require_conversation(want)
        rows = self.list_conversations()
        norm = want.lstrip("#").strip().lower()
        exact = [r for r in rows if r["name"].lower().lstrip("#") == norm
                 or (r.get("channel") or "").lower() == norm]
        if len(exact) == 1:
            return exact[0]
        loose = exact or [r for r in rows if norm and norm in r["name"].lower()]
        if len(loose) == 1:
            return loose[0]
        names = ", ".join(r["name"] for r in rows[:60]) or "(none)"
        reason = "matches several conversations" if loose else "was not found"
        raise ValueError(
            f"Conversation {conversation!r} {reason}. Conversations available here: {names}"
        )

    # ------------------------------------------------------------- messages

    def _sender(self, m: dict) -> str:
        prof = m.get("user_profile") or {}
        name = prof.get("display_name") or prof.get("real_name")
        if not name and m.get("user"):
            name = self._user_name(m.get("user"))
        return (name or m.get("username") or (m.get("bot_profile") or {}).get("name")
                or "Unknown")

    def _text(self, m: dict) -> str:
        text = str(m.get("text") or "")
        text = _USER_MENTION_RE.sub(lambda g: "@" + (g.group(2) or self._user_name(g.group(1)) or g.group(1)), text)
        text = _CHANNEL_MENTION_RE.sub(lambda g: "#" + (g.group(2) or g.group(1)), text)
        text = _SPECIAL_MENTION_RE.sub(lambda g: "@" + g.group(1), text)
        text = _SUBTEAM_RE.sub(lambda g: g.group(1) or "@group", text)
        text = _LINK_RE.sub(lambda g: f"{g.group(2)} ({g.group(1)})" if g.group(2) else g.group(1), text)
        text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        files = [f.get("name") or f.get("title") for f in (m.get("files") or []) if f.get("name") or f.get("title")]
        if files:
            text = (text + "\n" if text else "") + "Attached: " + ", ".join(files)
        return text.strip()

    @staticmethod
    def _is_user_message(m: dict) -> bool:
        return m.get("type", "message") == "message" and m.get("subtype") not in _SKIP_SUBTYPES and bool(m.get("ts"))

    def _row(self, m: dict, conv: dict, *, text: Optional[str] = None,
             web_url: Optional[str] = None) -> dict:
        body = text if text is not None else self._text(m)
        sender = self._sender(m)
        snippet = _snippet(body) or "(no text)"
        ts = str(m["ts"])
        thread_ts = m.get("thread_ts")
        cid = conv["channel_id"]
        return {
            "id": f"slack:{cid}/{ts}",
            "name": f"{sender}: {snippet}",
            "path": f"{conv['name'].translate(_PATH_UNSAFE)}/{sender.translate(_PATH_UNSAFE)}: {snippet.translate(_PATH_UNSAFE)}",
            "mime_type": "text/plain",
            "sender": sender,
            "modified_at": ts_to_iso(ts),
            "web_url": web_url or self.permalink(cid, ts, thread_ts),
            "preview": " ".join(body.split())[:_PREVIEW_CHARS] or None,
            "folder": conv["name"],
            "conversation_id": conv["id"],
            "reply_count": m.get("reply_count") or None,
            "is_reply": bool(thread_ts and thread_ts != ts),
        }

    def _thread_replies(self, conv: dict, roots: List[dict]) -> Dict[str, List[dict]]:
        """Replies of each threaded root (oldest first), fetched in parallel.

        ``conversations.history`` returns only a channel's top-level messages;
        a thread's replies need ``conversations.replies``. One page per thread,
        and at most ``_MAX_THREAD_EXPANSIONS`` threads per client instance, so
        a busy channel can't turn one listing into hundreds of calls.
        """
        todo = []
        with self._expansions_lock:
            for m in roots:
                if self._expansions_left <= 0:
                    break
                self._expansions_left -= 1
                todo.append(str(m["ts"]))
        if not todo:
            return {}

        def one(ts: str) -> Tuple[str, List[dict]]:
            try:
                msgs = self._thread(conv["channel_id"], ts, max_pages=1)
            except Exception as exc:  # replies are context; never fail the listing
                logger.info("slack: replies of %s/%s unavailable: %s", conv["channel_id"], ts, exc)
                return ts, []
            return ts, [r for r in msgs if str(r.get("ts")) != ts]

        with ThreadPoolExecutor(max_workers=_FANOUT_WORKERS) as pool:
            return dict(pool.map(one, todo))

    def _history_walk(self, conv: dict, after: Optional[datetime], before: Optional[datetime],
                      sender: Optional[str], n: int, state: dict, max_pages: int,
                      expand_threads: bool = True) -> Tuple[List[dict], Optional[dict]]:
        """Up to ``n`` messages of one conversation, newest first.

        Like Teams, a threaded post is followed by its replies (oldest first)
        so a discussion reads together — replies inside the time window and
        matching the sender filter, each marked as a reply. A thread is never
        split across pages: the cursor resumes at its root.

        ``state`` is ``{"c": slack cursor of the page to resume, "o": index
        within it}``; returns ``(rows, resume_state_or_None)``.
        """
        page_cursor = state.get("c")
        skip = int(state.get("o") or 0)
        rows: List[dict] = []
        for _ in range(max_pages):
            data = self._call(
                "conversations.history", channel=conv["channel_id"], limit=_PAGE_SIZE,
                oldest=_to_ts(after), latest=_to_ts(before), cursor=page_cursor,
            )
            messages = data.get("messages") or []
            roots = [m for m in messages[skip:] if self._is_user_message(m) and m.get("reply_count")
                     and str(m.get("thread_ts") or "") == str(m.get("ts"))]
            replies = self._thread_replies(conv, roots) if expand_threads else {}
            for i in range(skip, len(messages)):
                m = messages[i]
                if not self._is_user_message(m):
                    continue
                # A reply also sent to the channel shows up here too; it is
                # already listed under its root when that thread was expanded.
                if m.get("thread_ts") and m.get("thread_ts") != m.get("ts") and str(m["thread_ts"]) in replies:
                    continue
                unit = [self._row(m, conv)]
                for r in replies.get(str(m["ts"]), []):
                    if not self._is_user_message(r):
                        continue
                    when = parse_mail_datetime(ts_to_iso(r.get("ts")))
                    if (after and when and when < after) or (before and when and when >= before):
                        continue
                    unit.append(self._row(r, conv))
                if sender:
                    unit = [r for r in unit if sender.lower() in (r["sender"] or "").lower()]
                if not unit:
                    continue
                if rows and len(rows) + len(unit) > n:
                    return rows, {"c": page_cursor, "o": i}
                rows.extend(unit)
            skip = 0
            nxt = (data.get("response_metadata") or {}).get("next_cursor") or None
            if not nxt or not data.get("has_more", True):
                return rows, None
            page_cursor = nxt
        return rows, {"c": page_cursor, "o": 0}

    def list_messages(
        self,
        conversation: Optional[str] = None,
        from_user: Optional[str] = None,
        sent_after: Optional[str] = None,
        sent_before: Optional[str] = None,
        max_results: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Messages newest first (thread replies are counted on their parent;
        read the thread for them). Returns ``{items, next_cursor, folder}``."""
        if not self.access_token:
            return {"items": [], "next_cursor": None, "folder": None}
        n = clamp_mail_results(max_results)
        after = parse_mail_datetime(sent_after)
        before = parse_mail_datetime(sent_before)
        state = decode_cursor(cursor)

        if conversation:
            conv = self.resolve_conversation(conversation)
            rows, resume = self._history_walk(conv, after, before, from_user, n, state, _MAX_PAGES)
            return {"items": rows, "next_cursor": encode_cursor(resume) if resume else None,
                    "folder": self._folder(conv)}

        # No conversation: the newest messages across conversations (bounded).
        convs = self.list_conversations()
        if not convs:
            return {"items": [], "next_cursor": None, "folder": None}
        scanned = convs[:_FANOUT_CONVERSATIONS]
        with self._expansions_lock:
            self._expansions_left = min(self._expansions_left, _FANOUT_THREAD_EXPANSIONS)

        def one(conv):
            try:
                return self._history_walk(conv, after, before, from_user, _FANOUT_PAGE, {}, 1)
            except Exception as exc:
                logger.warning("slack: listing %s failed: %s", conv["id"], exc)
                return [], None

        with ThreadPoolExecutor(max_workers=_FANOUT_WORKERS) as pool:
            results = list(pool.map(one, scanned))
        merged: List[dict] = []
        partial: List[str] = []
        for conv, (rows, resume) in zip(scanned, results):
            merged.extend(rows)
            if resume:
                partial.append(conv["name"])
        partial.extend(c["name"] for c in convs[_FANOUT_CONVERSATIONS:])
        merged.sort(key=lambda r: r.get("modified_at") or "", reverse=True)
        skip = int(state.get("o") or 0)
        page = merged[skip: skip + n]
        nxt = encode_cursor({"o": skip + n}) if len(merged) > skip + n else None
        return {"items": page, "next_cursor": nxt, "folder": None, "partial": partial}

    @staticmethod
    def _folder(conv: dict) -> dict:
        return {"id": conv["id"], "name": conv["name"], "path": conv["name"], "kind": conv.get("kind")}

    # --------------------------------------------------------------- search

    def _scoped_channel_names(self) -> List[str]:
        """Current names of the allowed channels, for Slack-side ``in:#x``
        scoping of a search — so Slack returns matches from those channels
        instead of a workspace-wide page we'd mostly throw away. Empty (search
        everything, filter by id) when there is no channel scope, when DMs are
        in scope too (a DM can't be named with ``in:#``), or when the list is
        too long for one query. Names are read live from the channel ids, so a
        renamed channel still matches."""
        if not self.allowed or self.include_dms:
            return []
        rows = [r for r in self.list_conversations() if r.get("kind") in ("channel", "private channel")]
        if not rows or len(rows) > _MAX_IN_FILTERS:
            return []
        return [r["channel"] for r in rows if r.get("channel")]

    def _search_query(self, q: str, conv: Optional[dict], after: Optional[datetime],
                      before: Optional[datetime], scoped: bool = True) -> str:
        parts = [q]
        if conv is not None:
            if conv.get("kind") in ("channel", "private channel") and conv.get("channel"):
                parts.append(f"in:#{conv['channel']}")
        elif scoped:
            parts.extend(f"in:#{name}" for name in self._scoped_channel_names())
        # Slack's after:/before: are day-granular and exclusive; widen by a
        # day and filter exactly on each match's ts.
        if after:
            parts.append(f"after:{(after - timedelta(days=1)).date().isoformat()}")
        if before:
            parts.append(f"before:{(before + timedelta(days=1)).date().isoformat()}")
        return " ".join(parts)

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
        """``search.messages`` over every conversation the user can see.

        Slack enforces the user's own access; the agent's channel scope and
        DM setting, the conversation, sender and exact time window are then
        applied to each match (by channel id, never by name). Like Teams:

        - Slack requires every word of a query in one message, so a plain
          question finds nothing; an empty first search is retried with the
          question's distinctive words one at a time (``broadened_query``).
        - Each hit carries its thread: the post it answers and the replies
          around it (``thread`` / ``context`` / ``more_after``), so a single
          line of a discussion can be judged without opening every thread.
        """
        if not self.access_token:
            return {"items": [], "next_cursor": None, "folder": None}
        q = (query or "").strip()
        if not q:
            raise ValueError("search_chat_messages needs a non-empty query; use list_chat_messages to browse.")
        n = clamp_mail_results(max_results)
        after = parse_mail_datetime(sent_after)
        before = parse_mail_datetime(sent_before)
        conv = self.resolve_conversation(conversation) if conversation else None
        visible = {r["channel_id"]: r for r in self.list_conversations()}
        flt = (visible, conv, after, before, from_user)

        result = self._search_walk(q, conv, after, before, n, decode_cursor(cursor), flt)
        if not result["items"] and not cursor and conv is None and len(self._scoped_channel_names()) > 1:
            # Several in:#x filters in one query: if Slack reads them as AND
            # rather than OR, nothing comes back — search everything and keep
            # the allowed channels by id instead.
            result = self._search_walk(q, conv, after, before, n, {}, flt, scoped=False)
        if not result["items"] and not cursor:
            terms = slack_broadened_terms(q)
            if terms:
                result = self._search_any(terms, conv, after, before, n, flt)
                result["broadened_query"] = " OR ".join(terms)
        if not result["items"] and not cursor:
            named = self._conversation_named(q)
            if named is not None:
                # Searching for a channel's NAME finds messages that mention
                # it, not the channel's messages — say so instead of letting
                # "no results" read as "the channel is empty".
                result["hint"] = (
                    f"'{q}' is the name of the conversation {named['name']}, not a word in "
                    "its messages. To read that conversation, call list_chat_messages with "
                    f"conversation='{named['name']}' (and sent_after for a time window); to "
                    f"search inside it, pass conversation='{named['name']}' with topic keywords."
                )
        self._annotate_hits(result["items"])
        return result

    def _match_row(self, mt: dict, flt: tuple) -> Optional[dict]:
        """A search match as a message row, or None when the agent's
        scope, the conversation, the window or the sender rule it out."""
        visible, conv, after, before, from_user = flt
        row_conv = visible.get((mt.get("channel") or {}).get("id"))
        # Not a conversation this agent reads (outside its channel scope,
        # a DM with DMs off, or one the user left).
        if row_conv is None or (conv and row_conv["id"] != conv["id"]):
            return None
        ts = str(mt.get("ts") or "")
        if not ts:
            return None
        when = parse_mail_datetime(ts_to_iso(ts))
        if (after and when and when < after) or (before and when and when >= before):
            return None
        m = dict(mt)
        link = mt.get("permalink") or ""
        thread = _THREAD_TS_RE.search(link)
        if thread:
            m["thread_ts"] = thread.group(1)
        row = self._row(m, row_conv, web_url=link or None)
        if from_user and from_user.lower() not in (row["sender"] or "").lower():
            return None
        return row

    def _search_page(self, query: str, page: int) -> Tuple[List[dict], int]:
        data = self._call("search.messages", query=query, count=_SEARCH_PAGE, page=page,
                          sort="score", highlight="false")
        block = data.get("messages") or {}
        paging = block.get("paging") or block.get("pagination") or {}
        return block.get("matches") or [], int(paging.get("pages") or paging.get("page_count") or 1)

    def _search_walk(self, q: str, conv: Optional[dict], after, before, n: int,
                     state: dict, flt: tuple, scoped: bool = True) -> Dict[str, Any]:
        full_q = state.get("q") or self._search_query(q, conv, after, before, scoped=scoped)
        page = int(state.get("p") or 1)
        skip = int(state.get("o") or 0)
        items: List[dict] = []
        folder = self._folder(conv) if conv else None

        def done(nxt: Optional[dict]) -> Dict[str, Any]:
            cur = encode_cursor({**nxt, "q": full_q}) if nxt else None
            return {"items": items, "next_cursor": cur, "folder": folder}

        for _ in range(_MAX_SEARCH_PAGES):
            matches, pages = self._search_page(full_q, page)
            for i in range(skip, len(matches)):
                row = self._match_row(matches[i], flt)
                if row is None:
                    continue
                if len(items) >= n:
                    return done({"p": page, "o": i})
                items.append(row)
            skip = 0
            if page >= pages or not matches:
                return done(None)
            page += 1
        return done({"p": page, "o": 0})

    def _search_any(self, terms: List[str], conv: Optional[dict], after, before, n: int,
                    flt: tuple) -> Dict[str, Any]:
        """One page per term; messages matching more terms rank first, then
        Slack's own relevance order. Not paged (one bounded pass)."""
        found: Dict[str, Tuple[int, int, dict]] = {}
        rank = 0
        for term in terms:
            matches, _pages = self._search_page(self._search_query(term, conv, after, before), 1)
            for mt in matches:
                row = self._match_row(mt, flt)
                if row is None:
                    continue
                rank += 1
                hits, first, kept = found.get(row["id"], (0, rank, row))
                found[row["id"]] = (hits + 1, first, kept)
        ordered = sorted(found.values(), key=lambda v: (-v[0], v[1]))
        return {"items": [row for _h, _r, row in ordered[:n]], "next_cursor": None,
                "folder": self._folder(conv) if conv else None}

    def _context_line(self, m: dict, arrow: str) -> str:
        return f"{arrow} {self._sender(m)}: {_snippet(self._text(m), _CONTEXT_CHARS)}"

    def _annotate_hits(self, items: List[dict]) -> None:
        """Give each search hit its thread, as Teams does: for a reply, the
        post it answers and the replies around it; for a post, its first
        replies. A hit is one line of a discussion — the reason, the numbers
        and the final decision are usually elsewhere in it. Bounded to
        ``_SEARCH_CONTEXT_THREADS`` threads per search, fetched in parallel."""
        wanted: List[Tuple[str, str]] = []
        for row in items:
            cid, ts = row["conversation_id"].split(":", 1)[1], row["id"].rsplit("/", 1)[1]
            m = _THREAD_TS_RE.search(row.get("web_url") or "")
            key = (cid, m.group(1) if (row.get("is_reply") and m) else ts)
            if key not in wanted and len(wanted) < _SEARCH_CONTEXT_THREADS:
                wanted.append(key)
        if not wanted:
            return

        def one(key: Tuple[str, str]):
            try:
                return key, self._thread(key[0], key[1], max_pages=1)
            except Exception as exc:  # context is a bonus; never fail the search
                logger.info("slack: thread context unavailable for %s/%s: %s", key[0], key[1], exc)
                return key, []

        with ThreadPoolExecutor(max_workers=_FANOUT_WORKERS) as pool:
            threads = dict(pool.map(one, wanted))
        for row in items:
            cid, ts = row["conversation_id"].split(":", 1)[1], row["id"].rsplit("/", 1)[1]
            m = _THREAD_TS_RE.search(row.get("web_url") or "")
            root_ts = m.group(1) if (row.get("is_reply") and m) else ts
            msgs = threads.get((cid, root_ts)) or []
            if not msgs or str(msgs[0].get("ts")) != root_ts:
                continue
            root, replies = msgs[0], msgs[1:]
            if not replies:
                continue
            row["reply_count"] = len(replies)
            if ts == root_ts:
                row["context"] = [self._context_line(r, "↓") for r in replies[:_CONTEXT_AFTER]]
                row["more_after"] = max(0, len(replies) - _CONTEXT_AFTER)
                continue
            row["thread"] = f"{self._sender(root)}: {_snippet(self._text(root), 160)}"
            pos = next((i for i, r in enumerate(replies) if str(r.get("ts")) == ts), None)
            if pos is not None:
                row["context"] = (
                    [self._context_line(r, "↑") for r in replies[max(0, pos - _CONTEXT_BEFORE):pos]]
                    + [self._context_line(r, "↓") for r in replies[pos + 1: pos + 1 + _CONTEXT_AFTER]]
                )
                row["more_after"] = max(0, len(replies) - (pos + 1 + _CONTEXT_AFTER))

    # ----------------------------------------------------------------- read

    def _thread(self, channel_id: str, ts: str, max_pages: int = _MAX_PAGES) -> List[dict]:
        out: List[dict] = []
        cursor: Optional[str] = None
        for _ in range(max_pages):
            data = self._call("conversations.replies", channel=channel_id, ts=ts,
                              limit=_PAGE_SIZE, cursor=cursor)
            out.extend(m for m in data.get("messages") or [] if self._is_user_message(m))
            cursor = (data.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor or not data.get("has_more", True):
                break
        return out

    def _read_thread(self, conv: dict, ts: str) -> str:
        cid = conv["channel_id"]
        msgs = self._thread(cid, ts)
        if msgs and msgs[0].get("thread_ts") and msgs[0]["thread_ts"] != msgs[0].get("ts"):
            # The requested message is a reply: read its whole thread.
            msgs = self._thread(cid, msgs[0]["thread_ts"])
        if not msgs:
            raise ValueError("That Slack message could not be found — it may have been deleted.")
        root = msgs[0]
        if len(msgs) == 1 and not root.get("reply_count"):
            return self._read_window(conv, root)
        replies = msgs[1:]
        lines = [
            f"Slack {conv.get('kind') or 'conversation'}: {conv['name']}",
            f"Thread started by {self._sender(root)} on {_fmt_time(root.get('ts'))}"
            f" · {len(replies)} repl{'y' if len(replies) == 1 else 'ies'}",
            f"Link: {self.permalink(cid, str(root['ts']))}",
            "",
        ]
        for m in msgs:
            marker = "▶ " if m.get("ts") == ts and ts != root.get("ts") else ""
            indent = "" if m is root else "    ↳ "
            lines.append(f"{indent}{marker}[{_fmt_time(m.get('ts'))}] {self._sender(m)}: "
                         f"{self._text(m) or '(no text)'}")
        return "\n".join(lines)

    def _read_window(self, conv: dict, target: dict) -> str:
        """An unthreaded message with the messages right before and after it."""
        cid, ts = conv["channel_id"], str(target["ts"])
        before = self._call("conversations.history", channel=cid, latest=ts, inclusive="false",
                            limit=_WINDOW).get("messages") or []
        after = self._call("conversations.history", channel=cid, oldest=ts, inclusive="false",
                           limit=_WINDOW).get("messages") or []
        window = sorted(
            [m for m in before + [target] + after if self._is_user_message(m)],
            key=lambda m: float(m.get("ts") or 0),
        )
        lines = [
            f"Slack {conv.get('kind') or 'conversation'}: {conv['name']}",
            f"Link: {self.permalink(cid, ts)}",
            f"Showing {len(window)} message(s) around the requested one (▶).",
            "",
        ]
        for m in window:
            marker = "▶ " if m.get("ts") == ts else ""
            lines.append(f"{marker}[{_fmt_time(m.get('ts'))}] {self._sender(m)}: {self._text(m) or '(no text)'}")
        return "\n".join(lines)

    def _read_recent(self, conv: dict, limit: int = 50) -> str:
        rows, _resume = self._history_walk(conv, None, None, None, limit, {}, 1)
        lines = [f"Slack {conv.get('kind') or 'conversation'}: {conv['name']}",
                 f"Link: {conv.get('web_url')}",
                 f"Newest {len(rows)} message(s), newest first; replies under their post.", ""]
        for r in rows:
            replies = f" ({r['reply_count']} replies)" if r.get("reply_count") else ""
            indent = "    ↳ " if r.get("is_reply") else ""
            lines.append(f"{indent}[{_fmt_time_iso(r.get('modified_at'))}] {r['sender']}{replies}: "
                         f"{r.get('preview') or ''}  [id={r['id']}]")
        return "\n".join(lines)

    def read_file(self, file_id: str, **_) -> Any:
        parts = parse_slack_id(file_id)
        conv = self._require_conversation(parts["channel_id"])
        if not parts["ts"]:
            return self._read_recent(conv)
        return self._read_thread(conv, parts["ts"])

    # ----------------------------------------------- file-shaped compatibility

    @property
    def read_raw_bytes(self):
        raise AttributeError("Slack messages have no original bytes to preview")

    def list_files(self, folder_id: Optional[str] = None, recursive: Optional[bool] = None) -> List[dict]:
        """The browse view: newest messages, each under its conversation."""
        return self.list_messages(conversation=folder_id, max_results=200)["items"]

    def search_files(self, query: str, max_results: Optional[int] = None, **_) -> List[dict]:
        return self.search_messages(query, max_results=max_results)["items"]

    # Slack messages have no pre-indexed catalog — searched / read live per user.
    @discovery_progress
    def get_schemas(self, *args, progress_callback=None, **kwargs) -> List:
        return []

    def get_schema(self, table_name: str):
        return None

    def prompt_schema(self) -> str:
        return "Slack is searched and read live for the signed-in user."

    def execute_query(self, query: Optional[str] = None, table_name: Optional[str] = None, **kwargs):
        """No query language: a message/conversation id reads it, text searches."""
        target = kwargs.get("message_id") or kwargs.get("file_id") or table_name
        if target:
            return self.read_file(str(target))
        if query:
            return self.search_messages(str(query))
        raise ValueError("Slack has no query language — use search_chat_messages or list_chat_messages.")

    def test_connection(self) -> dict:
        # Admin-only (OAuth app saved, no user token yet): nothing to call as a
        # user. Verify on each user's sign-in instead — same as Gmail.
        if not self.access_token:
            return {
                "success": True,
                "message": "Slack app saved. Each user signs in with Slack to search their own channels.",
            }
        try:
            me = self._whoami()
        except Exception as e:
            return {"success": False, "message": str(e)}
        who = f"{me.get('user') or 'Slack user'} on {me.get('team') or 'Slack'}"
        if self._last_scopes is not None:
            needed = set(REQUIRED_SCOPES) | (set(DM_SCOPES) if self.include_dms else set())
            missing = sorted(needed - self._last_scopes)
            if missing:
                return {"success": False, "message": (
                    f"Signed in as {who}, but the Slack app is missing user token scopes: "
                    f"{', '.join(missing)}. Add them under OAuth & Permissions → User Token "
                    "Scopes, then sign in again."
                )}
        try:
            convs = self.list_conversations()
        except Exception as e:
            return {"success": False, "message": f"Signed in as {who}, but Slack is unreadable: {e}"}
        return {"success": True, "message": f"Connected as {who} · {len(convs)} conversation(s)"}


def _fmt_time_iso(value: Optional[str]) -> str:
    text = str(value or "")
    return text[:16].replace("T", " ") + " UTC" if len(text) >= 16 else text

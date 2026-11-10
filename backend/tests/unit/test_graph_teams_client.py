"""Microsoft Teams as a knowledge source: search, browse and read threads live.

The contract, independent of any one tenant:

* search pages to completion — following ``next_cursor`` yields every hit
  exactly once, for any result count and page size;
* any message (a channel root, a channel reply, a chat line) opens as its whole
  conversation context: a channel thread is the root plus EVERY reply, in
  order, wherever the thread sits in the channel; a chat read is the window
  around the message, oldest first; the requested message is marked;
* browsing a conversation pages every visible message exactly once, replies
  right after their root, and window / sender filters return exactly the
  matching set;
* system events and deleted messages never surface;
* a conversation resolves by id, channel name, chat topic or a person;
* no user token → empty results (the admin's credential test), never a 400;
* a cursor coming back through the model can never send the bearer token to a
  host other than Graph.

Microsoft Graph is replaced by a fake that implements the documented paging
(``$top`` + ``@odata.nextLink``), the channel ``$expand=replies`` shape, the
fact that a REPLY cannot be fetched by id on the channel messages endpoint
(Graph answers 404), and Microsoft Search's ``from``/``size`` paging with
member-only visibility.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.data_sources.clients.base import Capability
from app.data_sources.clients.graph_teams_client import GraphTeamsClient, parse_teams_id

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
ME = "u-me"
PEOPLE = {"u-me": "Dana Analyst", "u-omer": "Omer Finance", "u-lee": "Lee Ops"}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _msg(mid: int, who: str, text: str, minutes: int, *, kind: str = "message", html: bool = False, deleted=False):
    return {
        "id": str(mid),
        "messageType": kind,
        "createdDateTime": _iso(T0 + timedelta(minutes=minutes)),
        "deletedDateTime": _iso(T0) if deleted else None,
        "from": {"user": {"id": who, "displayName": PEOPLE[who]}} if kind == "message" else None,
        "body": {"contentType": "html" if html else "text", "content": f"<p>{text}</p>" if html else text},
        "webUrl": f"https://teams.microsoft.com/l/message/{mid}",
    }


class FakeTeams:
    """Graph Teams endpoints with documented paging / visibility semantics."""

    TEAM = {"id": "team-1", "displayName": "Finance"}

    def __init__(self, n_threads: int = 5, replies_per_thread: int = 3, n_chat: int = 7):
        self.requests: list[str] = []
        self.channels = {"chan-gen": "General", "chan-close": "Month-end close"}
        # Channel threads: root ids are ms-style increasing numbers; replies
        # are newer than their root.
        self.threads: dict[str, list[dict]] = {}  # channel -> roots (with _replies)
        mid = 1000
        for ch_i, ch in enumerate(self.channels):
            roots = []
            for t in range(n_threads):
                minute = (ch_i * 1000) + t * 30
                root = _msg(mid, "u-omer" if t % 2 else "u-me", f"{self.channels[ch]} topic {t} revenue", minute)
                mid += 1
                reps = []
                for r in range(replies_per_thread):
                    reps.append(_msg(mid, "u-lee" if r % 2 else "u-omer", f"reply {r} on {ch} topic {t}", minute + r + 1))
                    mid += 1
                root["_replies"] = reps
                roots.append(root)
            # a system event that must never surface
            roots.append(_msg(mid, "u-me", "<systemEventMessage/>", -5, kind="systemEventMessage"))
            mid += 1
            self.threads[ch] = roots
        # Chats the user is in, plus one they are NOT in (must stay invisible).
        self.chats = {
            "chat-dm": {"chatType": "oneOnOne", "topic": None, "members": ["u-me", "u-omer"]},
            "chat-grp": {"chatType": "group", "topic": "Forecast 2027", "members": ["u-me", "u-omer", "u-lee"]},
            "chat-private": {"chatType": "oneOnOne", "topic": None, "members": ["u-omer", "u-lee"]},
        }
        self.chat_msgs: dict[str, list[dict]] = {}
        for c_i, cid in enumerate(self.chats):
            rows = []
            for i in range(n_chat):
                who = self.chats[cid]["members"][i % len(self.chats[cid]["members"])]
                rows.append(_msg(mid, who, f"{cid} line {i} margin", 5000 + c_i * 500 + i, html=(i % 3 == 0)))
                mid += 1
            rows.append(_msg(mid, "u-omer", "this was deleted", 5000 + c_i * 500 + n_chat, deleted=True))
            mid += 1
            self.chat_msgs[cid] = rows

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _page(url: httpx.URL, rows: list, top_default: int = 20):
        q = dict(urllib.parse.parse_qsl(url.query.decode()))
        top = min(int(q.get("$top", top_default)), 50)
        skip = int(q.get("$skiptoken", 0))
        body = {"value": rows[skip: skip + top]}
        if skip + top < len(rows):
            q["$skiptoken"] = str(skip + top)
            path = url.path.split("/v1.0", 1)[1]
            body["@odata.nextLink"] = f"https://graph.microsoft.com/v1.0{path}?" + urllib.parse.urlencode(q)
        return httpx.Response(200, json=body)

    def _visible_messages(self):
        """(conversation identity, message) for everything the user may see."""
        out = []
        for ch, roots in self.threads.items():
            for root in roots:
                for m in [root] + root.get("_replies", []):
                    out.append(({"channelIdentity": {"teamId": self.TEAM["id"], "channelId": ch}, "chatId": ch}, m))
        for cid, rows in self.chat_msgs.items():
            if ME not in self.chats[cid]["members"]:
                continue
            for m in rows:
                out.append(({"channelIdentity": {"channelId": cid}, "chatId": cid}, m))
        return out

    # -- router --------------------------------------------------------------

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        path = request.url.path.split("/v1.0", 1)[1]
        q = dict(urllib.parse.parse_qsl(request.url.query.decode()))
        if path == "/me":
            return httpx.Response(200, json={"id": ME, "displayName": PEOPLE[ME], "userPrincipalName": "dana@x.test"})
        if path == "/me/joinedTeams":
            return httpx.Response(200, json={"value": [self.TEAM]})
        if path == f"/teams/{self.TEAM['id']}/channels":
            return httpx.Response(200, json={"value": [
                {"id": cid, "displayName": name, "webUrl": f"https://teams/c/{cid}"} for cid, name in self.channels.items()]})
        if path == "/me/chats":
            assert q.get("$expand") == "members"
            rows = [{"id": cid, "chatType": c["chatType"], "topic": c["topic"],
                     "lastUpdatedDateTime": _iso(T0), "webUrl": f"https://teams/chat/{cid}",
                     "members": [{"userId": u, "displayName": PEOPLE[u]} for u in c["members"]]}
                    for cid, c in self.chats.items() if ME in c["members"]]
            return self._page(request.url, rows)
        m = re.fullmatch(r"/teams/([^/]+)/channels/([^/]+)/messages", path)
        if m and request.method == "GET":
            roots = list(reversed(self.threads[m.group(2)]))  # newest activity first
            if q.get("$expand") == "replies":
                roots = [{**{k: v for k, v in r.items() if k != "_replies"}, "replies": r.get("_replies", [])} for r in roots]
            else:
                roots = [{k: v for k, v in r.items() if k != "_replies"} for r in roots]
            return self._page(request.url, roots)
        m = re.fullmatch(r"/teams/([^/]+)/channels/([^/]+)/messages/([^/]+)", path)
        if m:
            for root in self.threads[m.group(2)]:
                if root["id"] == m.group(3):
                    return httpx.Response(200, json={k: v for k, v in root.items() if k != "_replies"})
            return httpx.Response(404, json={"error": {"code": "NotFound", "message": "UnknownError"}})
        m = re.fullmatch(r"/teams/([^/]+)/channels/([^/]+)/messages/([^/]+)/replies", path)
        if m:
            for root in self.threads[m.group(2)]:
                if root["id"] == m.group(3):
                    return self._page(request.url, list(reversed(root["_replies"])))
            return httpx.Response(404, json={"error": {"code": "NotFound"}})
        m = re.fullmatch(r"/chats/([^/]+)/messages", path)
        if m:
            cid = m.group(1)
            if ME not in self.chats[cid]["members"]:
                return httpx.Response(403, json={"error": {"code": "Forbidden"}})
            assert q.get("$orderby") == "createdDateTime desc"
            return self._page(request.url, list(reversed(self.chat_msgs[cid])))
        if path == "/search/query" and request.method == "POST":
            req = json.loads(request.content)["requests"][0]
            assert req["entityTypes"] == ["chatMessage"]
            # Microsoft Search semantics: every word must match, unless the
            # query is an OR of alternatives.
            qs = req["query"]["queryString"].lower()
            groups = [g.split() for g in qs.split(" or ")]

            def matches(content: str) -> bool:
                words = set(re.findall(r"[\w$%.]+", content.lower()))
                return any(all(w in words for w in g) for g in groups)
            term = qs
            hits = [(ident, msg) for ident, msg in self._visible_messages()
                    if msg["messageType"] == "message" and not msg.get("deletedDateTime")
                    and matches(msg["body"]["content"])]
            start, size = req["from"], min(req["size"], 25)
            page = hits[start: start + size]
            return httpx.Response(200, json={"value": [{"hitsContainers": [{
                "total": len(hits),
                "moreResultsAvailable": start + size < len(hits),
                "hits": [{"hitId": msg["id"], "summary": f"<c0>{term}</c0> {msg['body']['content'][:40]}",
                          "resource": {"id": msg["id"], "createdDateTime": msg["createdDateTime"],
                                       "webLink": msg["webUrl"], "replyToId": None,
                                       "from": {"emailAddress": {"name": msg["from"]["user"]["displayName"]}},
                                       **ident}}
                         for ident, msg in page],
            }]}]})
        return httpx.Response(404, json={"error": {"code": "NotFound", "message": path}})


def _client(fake: FakeTeams, token: str | None = "delegated-test-token") -> GraphTeamsClient:
    client = GraphTeamsClient(access_token=token, tenant_id="t", client_id="c", client_secret="s")
    client._http = httpx.Client(transport=httpx.MockTransport(fake))
    return client


def _drain(call, **kwargs):
    ids, cursor, calls = [], None, 0
    while True:
        page = call(cursor=cursor, **kwargs)
        calls += 1
        ids += [r["id"] for r in page["items"]]
        assert len(page["items"]) <= (kwargs.get("max_results") or 100)
        cursor = page["next_cursor"]
        if not cursor:
            return ids
        assert calls < 500, "cursor never terminated"


# ------------------------------------------------------------- surface


def test_client_declares_only_the_chat_capabilities():
    """A Teams agent is offered the chat tools, never the file/mail ones."""
    assert GraphTeamsClient.capabilities == {
        Capability.LIST_CHAT_MESSAGES, Capability.READ_CHAT_THREAD, Capability.SEARCH_CHAT_MESSAGES,
    }


# ------------------------------------------------------------- search


@pytest.mark.parametrize("n_threads,replies", [(1, 0), (6, 4), (14, 6)])
@pytest.mark.parametrize("max_results", [1, 7, 25, 200])
def test_search_pages_to_every_visible_hit_exactly_once(n_threads, replies, max_results):
    fake = FakeTeams(n_threads=n_threads, replies_per_thread=replies)
    client = _client(fake)
    expected = {f"channel:team-1/{ch}/{m['id']}"
                for ch, roots in fake.threads.items() for r in roots for m in [r] + r.get("_replies", [])
                if "revenue" in m["body"]["content"]}
    ids = _drain(client.search_messages, query="revenue", max_results=max_results)
    assert len(ids) == len(set(ids)) == len(expected)
    assert set(ids) == expected


def test_search_hits_name_their_conversation_and_never_reach_a_private_chat():
    fake = FakeTeams()
    client = _client(fake)
    page = client.search_messages("margin", max_results=200)
    convs = {r["conversation_id"] for r in page["items"]}
    assert convs == {"chat:chat-dm", "chat:chat-grp"}
    assert all(r["folder"] in {"Chat with Omer Finance", "Forecast 2027"} for r in page["items"])
    assert all("<c0>" not in (r["preview"] or "") for r in page["items"])


def test_a_reply_hit_carries_the_post_it_answers_and_its_thread_size():
    """A hit is one line of a discussion. A reply comes back with the post it
    answers, so the cause/decision around it is visible from the hit list."""
    fake = FakeTeams(n_threads=5, replies_per_thread=3)
    client = _client(fake)
    items = client.search_messages("reply", max_results=200)["items"]
    assert items
    for row in items:
        root = next(r for ch in fake.threads.values() for r in ch
                    for m in r.get("_replies", []) if row["id"].endswith("/" + m["id"]))
        assert row["is_reply"] is True and row["reply_count"] == 3
        assert root["body"]["content"] in row["thread"]
    roots = client.search_messages("revenue", max_results=200)["items"]
    assert all(r.get("thread") is None and r["reply_count"] == 3 for r in roots)


@pytest.mark.parametrize("index", [0, 3, 6])
def test_a_chat_hit_carries_the_messages_around_it(index):
    """A question and its answer rarely share the searched words ("And gross
    margin?" → "71.4% for Q3"), so each chat hit brings its neighbours."""
    fake = FakeTeams(n_chat=7)
    client = _client(fake)
    rows = [m for m in fake.chat_msgs["chat-grp"] if not m.get("deletedDateTime")]
    target = rows[index]
    hit = next(r for r in client.search_messages(f"line {index} margin", max_results=200)["items"]
               if r["id"].endswith("/" + target["id"]))
    ctx = hit["context"]
    before = [m for m in rows[max(0, index - 2):index]]
    after = rows[index + 1:index + 4]
    assert hit["more_after"] == max(0, len(rows) - (index + 4))
    assert [c for c in ctx if c.startswith("↑")] == [f"↑ {m['from']['user']['displayName']}: {GraphTeamsClient._text(m)}" for m in before]
    assert [c for c in ctx if c.startswith("↓")] == [f"↓ {m['from']['user']['displayName']}: {GraphTeamsClient._text(m)}" for m in after]


def test_a_channel_post_hit_carries_its_first_replies_and_says_how_many_follow():
    fake = FakeTeams(n_threads=3, replies_per_thread=4)
    client = _client(fake)
    root = fake.threads["chan-gen"][1]
    hit = next(r for r in client.search_messages("revenue", max_results=200)["items"]
               if r["id"].endswith("/" + root["id"]))
    assert [c.split(": ", 1)[1] for c in hit["context"]] == [m["body"]["content"] for m in root["_replies"][:3]]
    assert hit["more_after"] == 1


@pytest.mark.parametrize("conversation", ["Forecast 2027", "chat:chat-grp", "forecast"])
def test_search_scoped_to_one_conversation_returns_only_its_hits(conversation):
    client = _client(FakeTeams())
    items = client.search_messages("margin", conversation=conversation, max_results=200)["items"]
    assert items and {r["conversation_id"] for r in items} == {"chat:chat-grp"}


def test_search_window_and_sender_filters_return_exactly_the_matching_set():
    fake = FakeTeams(n_threads=8, replies_per_thread=3)
    client = _client(fake)
    after = T0 + timedelta(minutes=60)
    before = T0 + timedelta(minutes=1150)
    items = client.search_messages(
        "revenue", from_user="omer", sent_after=after.isoformat(), sent_before=before.isoformat(), max_results=200,
    )["items"]
    expected = {m["id"] for roots in fake.threads.values() for r in roots for m in [r] + r.get("_replies", [])
                if "revenue" in m["body"]["content"] and m["from"]["user"]["displayName"] == "Omer Finance"
                and after <= datetime.fromisoformat(m["createdDateTime"].replace("Z", "+00:00")) < before}
    assert {r["id"].rsplit("/", 1)[1] for r in items} == expected


@pytest.mark.parametrize("query", ["what drove revenue growth", "revenue churn growth", "Q4 revenue forecast"])
def test_a_question_no_message_answers_word_for_word_still_finds_the_partial_matches(query):
    """Microsoft Search ANDs every word; a natural question rarely matches one
    message verbatim. When nothing matches all words, search retries with ANY
    of the meaningful ones and says so."""
    fake = FakeTeams(n_threads=4, replies_per_thread=2)
    client = _client(fake)
    page = client.search_messages(query, max_results=200)
    assert page["items"], "a broadened search must surface the messages that mention any term"
    assert page.get("broadened_query") and " OR " in page["broadened_query"]
    assert "what" not in page["broadened_query"].lower().split()
    assert all("revenue" in r["preview"] or "growth" in r["preview"] or "churn" in r["preview"]
               or "forecast" in r["preview"] or "q4" in r["preview"].lower() for r in page["items"])


def test_broadened_results_page_on_the_broadened_query():
    fake = FakeTeams(n_threads=20, replies_per_thread=1)
    client = _client(fake)
    ids = _drain(client.search_messages, query="revenue growth", max_results=7)
    expected = [m for roots in fake.threads.values() for r in roots for m in [r] + r.get("_replies", [])
                if "revenue" in m["body"]["content"]]
    assert len(ids) == len(set(ids)) == len(expected)


@pytest.mark.parametrize("query", ["revenue", '"topic 9 revenue"', "from:Omer growth", "revenue AND growth"])
def test_single_words_and_explicit_kql_are_never_rewritten(query):
    page = _client(FakeTeams()).search_messages(query, max_results=200)
    assert not page.get("broadened_query")


def test_empty_query_is_refused_with_a_pointer_to_browsing():
    with pytest.raises(ValueError, match="list_chat_messages"):
        _client(FakeTeams()).search_messages("   ")


# ------------------------------------------------------------- read


@pytest.mark.parametrize("thread_index", [0, 3, 13])
@pytest.mark.parametrize("which", ["root", "first_reply", "last_reply"])
def test_any_channel_message_reads_as_its_whole_thread(thread_index, which):
    """A search hit on ANY message of a thread — including a reply whose
    parent is not in the hit and sits pages deep — opens the full thread."""
    fake = FakeTeams(n_threads=14, replies_per_thread=4)
    client = _client(fake)
    root = fake.threads["chan-close"][thread_index]
    target = {"root": root, "first_reply": root["_replies"][0], "last_reply": root["_replies"][-1]}[which]
    text = client.read_file(f"channel:team-1/chan-close/{target['id']}")
    order = [root] + root.get("_replies", [])
    positions = [text.index(m["body"]["content"]) for m in order]
    assert positions == sorted(positions), "thread must read oldest first, root on top"
    assert "Month-end close" in text and "Link: " in text
    # nothing from another thread leaks in
    other = fake.threads["chan-close"][(thread_index + 1) % 14]
    assert other["body"]["content"] not in text
    marked = [line for line in text.splitlines() if "▶" in line]
    if which == "root":
        assert marked == []
    else:
        assert len(marked) == 1 and target["body"]["content"] in marked[0]


@pytest.mark.parametrize("index", [0, 3, 6])
def test_chat_message_reads_as_the_window_around_it(index):
    fake = FakeTeams(n_chat=30)
    client = _client(fake)
    target = fake.chat_msgs["chat-grp"][index]
    text = client.read_file(f"chat:chat-grp/{target['id']}")
    lines = [line for line in text.splitlines() if line.startswith(("[", "▶"))]
    assert any("▶" in line and f"line {index} " in line for line in lines)
    stamps = [re.search(r"\[(.*?)\]", line).group(1) for line in lines]
    assert stamps == sorted(stamps), "chat window must read oldest first"
    assert "Participants:" in text and "this was deleted" not in text


def test_conversation_id_reads_as_its_newest_messages():
    fake = FakeTeams()
    text = _client(fake).read_file("channel:team-1/chan-gen")
    assert "General" in text and fake.threads["chan-gen"][-2]["body"]["content"] in text


@pytest.mark.parametrize("bad", ["", "msg-123", "channel:only-team", "chat:", "folder:x/y", "channel:a//b"])
def test_unknown_ids_are_refused_with_guidance(bad):
    with pytest.raises(ValueError, match="search_chat_messages"):
        parse_teams_id(bad)


# ------------------------------------------------------------- browse


@pytest.mark.parametrize("n_threads,replies", [(2, 0), (12, 5), (30, 2)])
@pytest.mark.parametrize("max_results", [1, 9, 100])
def test_channel_listing_pages_every_message_once_with_replies_after_their_root(n_threads, replies, max_results):
    fake = FakeTeams(n_threads=n_threads, replies_per_thread=replies)
    client = _client(fake)
    ids = _drain(client.list_messages, conversation="General", max_results=max_results)
    expected = [m["id"] for r in reversed(fake.threads["chan-gen"]) if r["messageType"] == "message"
                for m in [r] + r.get("_replies", [])]
    assert [i.rsplit("/", 1)[1] for i in ids] == expected


def test_listing_hides_system_events_and_deleted_messages():
    client = _client(FakeTeams())
    text = json.dumps(_drain(client.list_messages, conversation="General")) + json.dumps(
        client.list_messages(conversation="chat:chat-dm", max_results=200)["items"])
    assert "systemEventMessage" not in text and "this was deleted" not in text


def test_chat_listing_window_and_sender_filters_return_exactly_the_matching_set():
    fake = FakeTeams(n_chat=40)
    client = _client(fake)
    rows = fake.chat_msgs["chat-grp"]
    after = rows[5]["createdDateTime"]
    before = rows[30]["createdDateTime"]
    items = client.list_messages(conversation="Forecast 2027", from_user="Lee", sent_after=after,
                                 sent_before=before, max_results=200)["items"]
    expected = [m["id"] for m in reversed(rows) if not m.get("deletedDateTime")
                and m["from"]["user"]["displayName"] == "Lee Ops" and after <= m["createdDateTime"] < before]
    assert [i["id"].rsplit("/", 1)[1] for i in items] == expected


def test_listing_without_a_conversation_merges_every_conversation_newest_first():
    fake = FakeTeams()
    items = _client(fake).list_messages(max_results=200)["items"]
    assert {i["conversation_id"] for i in items} == {
        "channel:team-1/chan-gen", "channel:team-1/chan-close", "chat:chat-dm", "chat:chat-grp"}
    stamps = [i["modified_at"] for i in items]
    assert stamps == sorted(stamps, reverse=True)


@pytest.mark.parametrize("name,expected", [
    ("General", "channel:team-1/chan-gen"),
    ("#general", "channel:team-1/chan-gen"),
    ("Finance › Month-end close", "channel:team-1/chan-close"),
    ("Forecast 2027", "chat:chat-grp"),
    ("Omer", "chat:chat-dm"),
])
def test_conversation_resolves_by_channel_topic_or_person(name, expected):
    assert _client(FakeTeams()).resolve_conversation(name)["id"] == expected


def test_unknown_conversation_names_the_real_ones():
    with pytest.raises(ValueError) as exc:
        _client(FakeTeams()).resolve_conversation("Board room")
    assert "General" in str(exc.value) and "Forecast 2027" in str(exc.value)


def test_browse_paths_group_messages_by_conversation():
    """The file browser builds one folder per conversation from `path`."""
    items = _client(FakeTeams()).list_files()
    folders = {i["path"].split("/", 1)[0] for i in items}
    assert folders == {"Finance › General", "Finance › Month-end close", "Chat with Omer Finance", "Forecast 2027"}
    assert all(i["path"].count("/") == 1 for i in items)


# ------------------------------------------------------------- auth & safety


def test_without_a_user_token_everything_is_empty_not_an_error():
    fake = FakeTeams()
    client = _client(fake, token=None)
    assert client.list_conversations() == []
    assert client.search_messages("revenue")["items"] == []
    assert client.list_messages()["items"] == []
    assert not [u for u in fake.requests if "/me" in u]


def test_cursor_cannot_redirect_the_bearer_token_off_graph():
    from app.data_sources.clients.mail_common import encode_cursor
    client = _client(FakeTeams())
    evil = encode_cursor({"u": "https://evil.example/steal"})
    with pytest.raises(ValueError, match="Invalid cursor"):
        client.list_messages(conversation="General", cursor=evil)


def test_connection_test_reports_who_and_how_many_teams():
    result = _client(FakeTeams()).test_connection()
    assert result["success"] and "dana@x.test" in result["message"]


# ------------------------------------------------------------- tool surface


@pytest.mark.asyncio
async def test_chat_tools_observation_shows_every_row_and_flags_incomplete(monkeypatch):
    from app.ai.tools.implementations import _file_tool_common
    from app.ai.tools.implementations.chat_tools import ListChatMessagesTool, SearchChatMessagesTool

    fake = FakeTeams(n_threads=20, replies_per_thread=3)
    client = _client(fake)

    async def _resolve(runtime_ctx, connection_id, capability):
        assert capability in (Capability.LIST_CHAT_MESSAGES, Capability.SEARCH_CHAT_MESSAGES)
        return client, None
    monkeypatch.setattr(_file_tool_common, "resolve_file_client", _resolve)

    async def run(tool, **kw):
        events = [e async for e in tool.run_stream({"connection_id": "c1", **kw}, {})]
        return events[-1].payload

    first = await run(SearchChatMessagesTool(), query="revenue", max_results=30)
    obs, out = first["observation"], first["output"]
    assert out["truncated"] and out["next_cursor"] in obs["summary"]
    assert all(f["id"] in obs["details"] for f in out["files"])
    assert "read_chat_thread" in obs["summary"]
    # The card shows the conversation beside each message.
    assert {f["path"] for f in out["files"]} <= {"Finance › General", "Finance › Month-end close"}

    rest = await run(SearchChatMessagesTool(), query="revenue", cursor=out["next_cursor"], max_results=200)
    total = len(out["files"]) + len(rest["output"]["files"])
    matching = [m for roots in fake.threads.values() for r in roots
                for m in [r] + r.get("_replies", []) if "revenue" in m["body"]["content"]]
    assert not rest["output"]["truncated"] and total == len(matching) > 30

    convs = await run(ListChatMessagesTool(), list_conversations=True)
    assert "Forecast 2027" in convs["observation"]["details"]
    assert "chat-private" not in convs["observation"]["details"]


@pytest.mark.asyncio
async def test_read_chat_thread_names_the_read_after_its_conversation(monkeypatch):
    """The read card must say WHERE the thread lives, not echo the opaque id."""
    from app.ai.tools.implementations import _file_tool_common, read_file
    from app.ai.tools.implementations.chat_tools import ReadChatThreadTool

    fake = FakeTeams(n_threads=3, replies_per_thread=2)
    client = _client(fake)
    reply = fake.threads["chan-close"][1]["_replies"][1]
    file_id = f"channel:team-1/chan-close/{reply['id']}"

    async def _resolve(runtime_ctx, connection_id, capability):
        assert capability == Capability.READ_CHAT_THREAD
        return client, None
    monkeypatch.setattr(_file_tool_common, "resolve_file_client", _resolve)
    monkeypatch.setattr(read_file, "resolve_file_client", _resolve, raising=False)

    events = [e async for e in ReadChatThreadTool().run_stream(
        {"connection_id": "c1", "file_id": file_id}, {"db": None, "organization": None})]
    out = events[-1].payload["output"]
    assert out["success"], out
    assert out["file_name"] == out["path"] == "Finance › Month-end close"
    assert reply["body"]["content"] in out["text"]


@pytest.mark.asyncio
async def test_message_reads_leave_no_copy_with_the_report(monkeypatch):
    """A thread read with the user's own token must not be saved as a report
    file — anyone the report is shared with could open the copy. The same read
    path still keeps a session copy for ordinary files (the control)."""
    from app.ai.tools.implementations import _file_tool_common, read_file
    from app.ai.tools.implementations.chat_tools import ReadChatThreadTool
    from app.ai.tools.implementations.email_tools import ReadEmailTool

    assert ReadChatThreadTool._keep_session_copy is False
    assert ReadEmailTool._keep_session_copy is False
    assert read_file.ReadFileTool._keep_session_copy is True

    fake = FakeTeams(n_threads=2, replies_per_thread=1)
    client = _client(fake)
    persisted = []

    async def _persist(runtime_ctx, **kw):
        persisted.append(kw)
        return "file-123"
    monkeypatch.setattr(read_file, "_persist_session_file", _persist)

    async def _resolve(runtime_ctx, connection_id, capability):
        return client, None
    monkeypatch.setattr(_file_tool_common, "resolve_file_client", _resolve)
    monkeypatch.setattr(read_file, "resolve_file_client", _resolve, raising=False)

    file_id = f"chat:chat-grp/{fake.chat_msgs['chat-grp'][2]['id']}"
    ctx = {"db": None, "organization": None}
    events = [e async for e in ReadChatThreadTool().run_stream({"connection_id": "c1", "file_id": file_id}, ctx)]
    out = events[-1].payload["output"]
    assert out["success"] and out.get("session_file_id") is None
    assert persisted == []

    class _FileTool(ReadChatThreadTool):
        _keep_session_copy = True
    events = [e async for e in _FileTool().run_stream({"connection_id": "c1", "file_id": file_id}, ctx)]
    assert persisted, "the control read must still persist a session copy"

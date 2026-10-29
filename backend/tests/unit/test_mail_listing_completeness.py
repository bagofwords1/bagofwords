"""Mailbox listing/search must be complete, filterable and folder-scoped.

A scheduled "summarise every email since yesterday" run reported a partial
mailbox as complete: each call stopped at 25 messages, ignored the folder it
was asked for, could not filter unread/date server-side, and a search with a
quoted phrase failed with a Graph 400. These tests pin the general contract,
not that incident:

* any mailbox size, any page size → following ``next_cursor`` yields every
  matching message exactly once, and the cursor is absent only when complete;
* folder / unread / received-window filters return exactly the matching set;
* quoted phrases are valid searches;
* rows carry a preview and read state for triage.

The external boundary (Microsoft Graph / Gmail REST) is replaced by fakes that
implement the documented paging and query rules — including the ones Graph
enforces with a 400 (``InefficientFilter`` ordering, ``$search`` combined with
``$filter``/``$orderby``, unescaped inner quotes) — so a client that violates
them fails here the way it would against the real service.
"""
from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.data_sources.clients.gmail_mail_client import GmailMailClient
from app.data_sources.clients.graph_mail_client import GraphMailClient

T0 = datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _mailbox(n_inbox: int, n_nested: int, seed: int = 7):
    """Messages newest-first across Inbox and a nested Inbox/Telex In folder."""
    msgs = []
    for i in range(n_inbox + n_nested):
        folder = "f-inbox" if i < n_inbox else "f-telex"
        msgs.append({
            "id": f"m{i:04d}",
            "subject": f"Report {i} " + ("main engine" if i % 5 == 0 else "routine"),
            "isRead": (i * seed) % 3 == 0,
            "receivedDateTime": _iso(T0 - timedelta(minutes=17 * i)),
            "parentFolderId": folder,
            "bodyPreview": f"  Body of message {i}\r\n with   detail  ",
        })
    msgs.sort(key=lambda m: m["receivedDateTime"], reverse=True)
    return msgs


class FakeGraph:
    """Graph mail endpoints with the documented paging/query semantics."""

    FOLDERS = [
        {"id": "f-inbox", "displayName": "Inbox", "parentFolderId": "root", "childFolderCount": 1},
        {"id": "f-ops", "displayName": "Ops", "parentFolderId": "root", "childFolderCount": 0},
        {"id": "f-telex", "displayName": "Telex In", "parentFolderId": "f-inbox", "childFolderCount": 0},
    ]

    def __init__(self, messages):
        self.messages = messages
        self.requests: list[httpx.URL] = []

    def _folder_row(self, f):
        inside = [m for m in self.messages if m["parentFolderId"] == f["id"]]
        return {**f, "totalItemCount": len(inside), "unreadItemCount": sum(not m["isRead"] for m in inside)}

    @staticmethod
    def _bad(msg):
        return httpx.Response(400, json={"error": {"code": "BadRequest", "message": msg}})

    def _page(self, request, rows):
        q = dict(urllib.parse.parse_qsl(request.url.query.decode()))
        top = min(int(q.get("$top", 10)), 1000)
        skip = int(q.get("$skip", 0))
        body = {"value": rows[skip: skip + top]}
        if skip + top < len(rows):
            q["$skip"] = str(skip + top)
            body["@odata.nextLink"] = (
                f"https://graph.microsoft.com/v1.0{request.url.path.split('/v1.0', 1)[1]}?"
                + urllib.parse.urlencode(q)
            )
        return httpx.Response(200, json=body)

    def _apply_filter(self, expr, rows):
        for clause in expr.split(" and "):
            m = re.fullmatch(r"receivedDateTime (ge|lt) (\S+)", clause)
            if m:
                bound = m.group(2)
                rows = [r for r in rows if (r["receivedDateTime"] >= bound if m.group(1) == "ge" else r["receivedDateTime"] < bound)]
            elif clause == "isRead eq false":
                rows = [r for r in rows if not r["isRead"]]
            else:
                raise ValueError(clause)
        return rows

    def _search(self, value, rows):
        if not (value.startswith('"') and value.endswith('"')):
            return None, "Syntax error: $search value must be quoted"
        inner = value[1:-1]
        if re.search(r'(?<!\\)"', inner):
            return None, "Syntax error: character '\"' is not valid at position 10."
        inner = inner.replace('\\"', '"')
        terms = [re.sub(r"^\w+:", "", t.strip()).strip('"').lower() for t in re.split(r"\s+OR\s+", inner)]
        return [r for r in rows if any(t in r["subject"].lower() for t in terms)], None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url)
        path = request.url.path.split("/v1.0", 1)[1]
        q = dict(urllib.parse.parse_qsl(request.url.query.decode()))
        if path == "/me/mailFolders":
            return httpx.Response(200, json={"value": [self._folder_row(f) for f in self.FOLDERS if f["parentFolderId"] == "root"]})
        m = re.fullmatch(r"/me/mailFolders/([^/]+)/childFolders", path)
        if m:
            return httpx.Response(200, json={"value": [self._folder_row(f) for f in self.FOLDERS if f["parentFolderId"] == m.group(1)]})
        m = re.fullmatch(r"/me/mailFolders/([^/]+)/messages", path)
        if m:
            rows = [r for r in self.messages if r["parentFolderId"] == m.group(1)]
        elif path == "/me/messages":
            rows = list(self.messages)
        else:
            return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
        if "$search" in q:
            if "$filter" in q or "$orderby" in q:
                return self._bad("$search can't be combined with $filter or $orderby")
            rows, err = self._search(q["$search"], rows)
            if err:
                return self._bad(err)
        if "$filter" in q:
            if q.get("$orderby", "").startswith("receivedDateTime") and not q["$filter"].startswith("receivedDateTime"):
                return self._bad("InefficientFilter: The restriction or sort order is too complex for this operation.")
            rows = self._apply_filter(q["$filter"], rows)
        return self._page(request, rows)


def _graph(messages):
    fake = FakeGraph(messages)
    client = GraphMailClient(access_token="delegated-test-token")
    client._http = httpx.Client(transport=httpx.MockTransport(fake))
    return client, fake


def _drain(call, **kwargs):
    """Follow next_cursor to completion; return (ids, calls)."""
    ids, calls, cursor = [], 0, None
    while True:
        page = call(cursor=cursor, **kwargs)
        calls += 1
        ids += [r["id"] for r in page["items"]]
        assert len(page["items"]) <= (kwargs.get("max_results") or 100)
        cursor = page["next_cursor"]
        if not cursor:
            return ids, calls
        assert calls < 1000, "cursor never terminated"


@pytest.mark.parametrize("n_inbox,n_nested", [(0, 0), (7, 3), (60, 41), (237, 0)])
@pytest.mark.parametrize("max_results", [1, 25, 100, 200])
def test_outlook_list_pages_to_every_message_exactly_once(n_inbox, n_nested, max_results):
    messages = _mailbox(n_inbox, n_nested)
    client, _ = _graph(messages)
    ids, _ = _drain(client.list_messages, max_results=max_results)
    assert ids == [m["id"] for m in messages]


def test_outlook_cursor_is_absent_only_when_complete():
    client, _ = _graph(_mailbox(130, 0))
    first = client.list_messages(max_results=100)
    assert len(first["items"]) == 100 and first["next_cursor"]
    rest = client.list_messages(max_results=100, cursor=first["next_cursor"])
    assert len(rest["items"]) == 30 and rest["next_cursor"] is None


@pytest.mark.parametrize("folder", ["Inbox/Telex In", "telex in", "f-telex"])
def test_outlook_folder_by_path_name_or_id_scopes_the_listing(folder):
    messages = _mailbox(40, 23)
    client, _ = _graph(messages)
    page = client.list_messages(folder=folder, max_results=200)
    expected = [m["id"] for m in messages if m["parentFolderId"] == "f-telex"]
    assert [r["id"] for r in page["items"]] == expected
    assert page["folder"]["path"] == "Inbox/Telex In"
    assert page["folder"]["total_count"] == len(expected)


def test_outlook_unknown_folder_names_the_real_ones():
    client, _ = _graph(_mailbox(5, 5))
    with pytest.raises(ValueError) as exc:
        client.list_messages(folder="Telex Out")
    assert "Inbox/Telex In" in str(exc.value) and "Ops" in str(exc.value)


@pytest.mark.parametrize("unread_only", [True, False])
@pytest.mark.parametrize("window_h", [None, (1, 30), (0, 72)])
def test_outlook_unread_and_window_filters_return_exactly_the_matching_set(unread_only, window_h):
    messages = _mailbox(150, 50)
    client, _ = _graph(messages)
    kwargs = {"unread_only": unread_only, "max_results": 50}
    lo = hi = None
    if window_h:
        hi = T0 - timedelta(hours=window_h[0])
        lo = T0 - timedelta(hours=window_h[1])
        kwargs.update(received_after=_iso(lo), received_before=_iso(hi))
    ids, _ = _drain(client.list_messages, **kwargs)
    expected = [
        m["id"] for m in messages
        if (not unread_only or not m["isRead"])
        and (lo is None or _iso(lo) <= m["receivedDateTime"] < _iso(hi))
    ]
    assert ids == expected


@pytest.mark.parametrize("query", [
    'routine OR "main engine"',
    '"main engine"',
    'subject:"main engine"',
])
def test_outlook_search_accepts_quoted_phrases_and_pages_to_completion(query):
    messages = _mailbox(180, 0)
    client, _ = _graph(messages)
    ids, calls = _drain(client.search_messages, query=query, max_results=40)
    phrase_only = "routine" not in query
    expected = [m["id"] for m in messages if "main engine" in m["subject"] or not phrase_only]
    assert ids == expected
    assert calls > 1 or len(expected) <= 40


def test_outlook_search_applies_unread_and_window_without_breaking_graph_rules():
    messages = _mailbox(200, 0)
    client, _ = _graph(messages)
    lo = T0 - timedelta(hours=20)
    ids, _ = _drain(client.search_messages, query="Report", unread_only=True,
                    received_after=_iso(lo), max_results=30)
    assert ids == [m["id"] for m in messages if not m["isRead"] and m["receivedDateTime"] >= _iso(lo)]


def test_outlook_rows_carry_preview_read_state_and_folder():
    client, _ = _graph(_mailbox(3, 2))
    rows = client.list_messages()["items"]
    assert all(r["preview"] and "  " not in r["preview"] and "\n" not in r["preview"] for r in rows)
    assert all(isinstance(r["is_read"], bool) for r in rows)
    assert {r["folder"] for r in rows} == {"Inbox", "Inbox/Telex In"}


def test_outlook_cursor_cannot_redirect_the_bearer_token_off_graph():
    from app.data_sources.clients.mail_common import encode_cursor

    client, fake = _graph(_mailbox(3, 0))
    with pytest.raises(ValueError):
        client.list_messages(cursor=encode_cursor({"u": "https://attacker.example/steal", "o": 0}))
    assert not any(u.host == "attacker.example" for u in fake.requests)


def test_outlook_file_shaped_entrypoints_are_no_longer_capped_at_25():
    messages = _mailbox(90, 0)
    client, _ = _graph(messages)
    assert len(client.list_files()) == 90
    assert len(client.search_files("Report", max_results=60)) == 60


# ------------------------------------------------------------------- Gmail


class FakeGmail:
    LABELS = [{"id": "INBOX", "name": "INBOX"}, {"id": "Label_7", "name": "Ops"}]

    def __init__(self, messages):
        self.messages = messages  # newest first; each has labels + epoch
        self.queries: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        p = request.url.params
        if path.endswith("/users/me/labels"):
            return httpx.Response(200, json={"labels": self.LABELS})
        m = re.search(r"/users/me/labels/([^/]+)$", path)
        if m:
            inside = [x for x in self.messages if m.group(1) in x["labels"]]
            return httpx.Response(200, json={"id": m.group(1), "messagesTotal": len(inside),
                                             "messagesUnread": sum("UNREAD" in x["labels"] for x in inside)})
        if path.endswith("/users/me/messages"):
            rows = list(self.messages)
            q = p.get("q", "")
            self.queries.append(q)
            if "is:unread" in q:
                rows = [x for x in rows if "UNREAD" in x["labels"]]
            for op, val in re.findall(r"(after|before):(\d+)", q):
                rows = [x for x in rows if (x["epoch"] > int(val) if op == "after" else x["epoch"] < int(val))]
            if p.get("labelIds"):
                rows = [x for x in rows if p["labelIds"] in x["labels"]]
            size = int(p.get("maxResults", 100))
            start = int(p.get("pageToken") or 0)
            body = {"messages": [{"id": x["id"], "threadId": "t" + x["id"]} for x in rows[start:start + size]]}
            if start + size < len(rows):
                body["nextPageToken"] = str(start + size)
            return httpx.Response(200, json=body)
        mid = path.rsplit("/", 1)[-1]
        x = next(x for x in self.messages if x["id"] == mid)
        return httpx.Response(200, json={
            "id": mid, "threadId": "t" + mid, "labelIds": x["labels"], "snippet": "Snippet &amp; more",
            "internalDate": str(x["epoch"] * 1000),
            "payload": {"headers": [{"name": "Subject", "value": f"S {mid}"}, {"name": "From", "value": "a@b.c"}]},
        })


def _gmail_box(n):
    out = []
    for i in range(n):
        labels = ["INBOX"] + (["UNREAD"] if i % 3 else []) + (["Label_7"] if i % 4 == 0 else [])
        out.append({"id": f"g{i:04d}", "labels": labels, "epoch": int((T0 - timedelta(minutes=13 * i)).timestamp())})
    return out


def _gmail(messages):
    fake = FakeGmail(messages)
    return GmailMailClient(access_token="t", transport=httpx.MockTransport(fake)), fake


@pytest.mark.parametrize("n,max_results", [(0, 25), (9, 25), (230, 100), (230, 200)])
def test_gmail_list_pages_to_every_message_exactly_once(n, max_results):
    box = _gmail_box(n)
    client, _ = _gmail(box)
    ids, _ = _drain(client.list_messages, max_results=max_results)
    assert ids == [x["id"] for x in box]


def test_gmail_unread_window_and_label_filters_are_server_side():
    box = _gmail_box(160)
    client, fake = _gmail(box)
    lo = T0 - timedelta(hours=10)
    ids, _ = _drain(client.list_messages, folder="Ops", unread_only=True,
                    received_after=_iso(lo), max_results=7)
    assert ids == [x["id"] for x in box
                   if "Label_7" in x["labels"] and "UNREAD" in x["labels"] and x["epoch"] > lo.timestamp()]
    assert all("is:unread" in q for q in fake.queries)


def test_gmail_rows_carry_preview_and_read_state():
    client, _ = _gmail(_gmail_box(4))
    rows = client.list_messages()["items"]
    assert rows[0]["preview"] == "Snippet & more"
    assert [r["is_read"] for r in rows] == [True, False, False, True]


# ------------------------------------------------------------- tool surface


@pytest.mark.asyncio
async def test_list_emails_observation_shows_every_row_and_flags_incomplete(monkeypatch):
    """The planner reads only the observation: every returned row (with its
    preview) must be in it, and an incomplete page must say how to continue."""
    from app.ai.tools.implementations import _file_tool_common
    from app.ai.tools.implementations.email_tools import ListEmailsTool

    messages = _mailbox(150, 0)
    client, _ = _graph(messages)

    # Connection resolution needs a report/DB; the unit under test is the
    # tool's paging contract, so hand it the (HTTP-faked) client directly.
    async def _resolve(runtime_ctx, connection_id, capability):
        return client, None
    monkeypatch.setattr(_file_tool_common, "resolve_file_client", _resolve)

    async def run(**kw):
        events = [e async for e in ListEmailsTool().run_stream({"connection_id": "c1", **kw}, {})]
        return events[-1].payload

    first = await run(max_results=120)
    obs, out = first["observation"], first["output"]
    assert out["truncated"] and out["next_cursor"]
    assert out["next_cursor"] in obs["summary"]
    assert all(m["id"] in obs["details"] for m in messages[:120])
    assert "Body of message 0" in obs["details"]

    second = await run(cursor=out["next_cursor"])
    assert not second["output"]["truncated"] and second["output"]["next_cursor"] is None
    assert [f["id"] for f in out["files"] + second["output"]["files"]] == [m["id"] for m in messages]

    folders = await run(list_folders=True)
    assert "Inbox/Telex In" in folders["observation"]["details"]


def test_outlook_folder_walk_terminates_on_a_cyclic_tree():
    """A misbehaving tenant/proxy returning a parent as its own child must not
    turn one listing into hundreds of folder requests."""
    client, fake = _graph(_mailbox(3, 0))
    loop = [{"id": "f-a", "displayName": "A", "childFolderCount": 1, "parentFolderId": "root"}]
    original = fake.__call__

    def cyclic(request):
        if "/mailFolders" in request.url.path and "/messages" not in request.url.path:
            fake.requests.append(request.url)
            return httpx.Response(200, json={"value": loop})
        return original(request)

    client._http = httpx.Client(transport=httpx.MockTransport(cyclic))
    assert [f["path"] for f in client.list_mail_folders()] == ["A"]
    assert len(fake.requests) <= 3

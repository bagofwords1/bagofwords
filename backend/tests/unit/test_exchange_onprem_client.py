"""Exchange Server mail connector contract; only the remote HTTP boundary is faked.

The provider behind these tests (``tests/fixtures/fake_exchange.py``) evaluates
each request against a mailbox model, so a test passes only when the connector
asked Exchange for the right thing — not when it happens to echo a canned reply.
"""
from datetime import datetime, timedelta, timezone

import pytest
import requests

from app.data_sources.clients import _exchange_ews
from app.data_sources.clients._exchange_ews import ExchangeError
from app.data_sources.clients.base import Capability
from app.data_sources.clients.exchange_onprem_client import ExchangeOnpremClient
from app.schemas.data_source_registry import REGISTRY, resolve_client_class
from app.schemas.data_sources.configs import ExchangeOnpremConfig
from tests.fixtures.fake_exchange import READ_OPERATIONS, FakeExchange, Message

URL = "https://mail.example.com/EWS/Exchange.asmx"
SUPPORT = "support@example.com"
SALES = "sales@example.com"
T0 = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)


def stamp(hours_ago: float) -> str:
    return (T0 - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def msg(subject, hours_ago, *, sender=("Dana Levi", "dana@example.com"), body="", read=True, **kw):
    return Message(subject=subject, sender_name=sender[0], sender_email=sender[1],
                   received=stamp(hours_ago), body=body, is_read=read, **kw)


class _Response:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body = status, body
        self.headers = headers or {"Content-Length": str(len(body))}

    def iter_content(self, size):
        for start in range(0, len(self._body), size):
            yield self._body[start:start + size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def exchange(monkeypatch):
    """A small Exchange organization served at the HTTP boundary."""
    _exchange_ews.forget_rejected_credentials()
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    server = FakeExchange()
    server.own.add_folder("Projects", parent="Inbox")
    server.own.add_folder("Apollo", parent="Inbox/Projects")
    server.own.add_folder("Archive 2025")
    server.own.add("Inbox", msg("Quarterly revenue report", 1, body="Revenue grew twenty percent.", read=False))
    server.own.add("Inbox", msg("Lunch on Thursday?", 5, sender=("Noa Cohen", "noa@example.com")))
    server.own.add("Inbox", msg("Vendor contract renewal", 30, body="The contract renews in March."))
    server.own.add("Inbox/Projects/Apollo", msg("Apollo launch checklist", 3, body="Launch readiness review.", read=False))
    server.own.add("Archive 2025", msg("Revenue forecast 2025", 400, body="Old revenue numbers."))
    server.own.add("Deleted Items", msg("Revenue spam offer", 2, body="Discarded revenue newsletter."))
    server.own.add("Junk Email", msg("You won revenue", 2))
    support = server.add_shared(SUPPORT)
    support.add_folder("Escalations", parent="Inbox")
    support.add("Inbox", msg("Customer cannot log in", 2, sender=("Acme Buyer", "buyer@acme.example"),
                             body="Login fails after the revenue dashboard update.", read=False))
    support.add("Inbox/Escalations", msg("Outage in billing", 4, body="Billing is down."))
    server.add_shared(SALES).add("Inbox", msg("Renewal quote", 6, body="Quote attached."))

    state = {"status": None, "raw": None, "posts": 0, "identities": [], "fail_network": 0, "verify": True}

    def post(session, url, data=None, **kw):
        state["posts"] += 1
        # The session rules every on-premises connector must follow.
        assert kw["allow_redirects"] is False
        assert session.verify == state["verify"]
        assert session.trust_env is False
        assert url == URL
        if state["fail_network"]:
            state["fail_network"] -= 1
            raise requests.ConnectionError("connection reset")
        state["identities"].append(getattr(session.auth, "username", None))
        if state["status"]:
            return _Response(state["status"], b"<html>denied</html>")
        if state["raw"] is not None:
            return _Response(200, state["raw"])
        status, body = server.handle(data)
        return _Response(status, body)

    monkeypatch.setattr(requests.Session, "post", post)
    monkeypatch.setattr(_exchange_ews.time, "sleep", lambda seconds: state.setdefault("slept", []).append(seconds))
    server.state = state
    return server


def client(**overrides):
    params = {"ews_url": "https://mail.example.com", "username": "CORP\\dana", "password": "secret",
              "shared_mailboxes": f"{SUPPORT}, {SALES}"}
    params.update(overrides)
    return ExchangeOnpremClient(**params)


def subjects(result):
    return [item["name"] for item in result["items"]]


def page_all(call, **kwargs):
    """Follow cursors to the end; returns every item and the number of calls."""
    items, cursor, calls = [], None, 0
    while True:
        result = call(cursor=cursor, **kwargs)
        calls += 1
        items.extend(result["items"])
        cursor = result["next_cursor"]
        if not cursor:
            return items, calls
        assert calls < 300, "paging never completed"


# ------------------------------------------------------------------ registration

def test_registry_offers_the_mail_tools_and_nothing_else():
    entry = REGISTRY["exchange_onprem"]
    assert resolve_client_class("exchange_onprem") is ExchangeOnpremClient
    assert ExchangeOnpremClient.capabilities == {
        Capability.LIST_EMAILS, Capability.READ_EMAIL, Capability.SEARCH_EMAILS,
    }
    assert entry.requires_license == "enterprise"
    assert set(entry.credentials_auth.by_auth) == {"ntlm", "kerberos"}
    assert "user" in entry.credentials_auth.by_auth["ntlm"].scopes
    assert entry.credentials_auth.by_auth["kerberos"].scopes == ["system"]


@pytest.mark.parametrize("given, expected", [
    ("https://mail.example.com", URL),
    ("https://mail.example.com/", URL),
    ("https://mail.example.com/EWS/Exchange.asmx", URL),
    ("https://mail.example.com:8443/custom/ews.asmx", "https://mail.example.com:8443/custom/ews.asmx"),
])
def test_server_url_gets_the_service_path_only_when_missing(given, expected):
    assert client(ews_url=given).ews_url == expected


@pytest.mark.parametrize("bad", [
    "http://mail.example.com",                 # plain HTTP without the lab switch
    "https://user:pw@mail.example.com",        # credentials in the URL
    "https://mail.example.com/EWS?x=1",        # query string
    "ftp://mail.example.com",
    "mail.example.com",
])
def test_unsafe_server_urls_are_refused_by_schema_and_client(bad):
    with pytest.raises(ValueError):
        client(ews_url=bad)
    with pytest.raises(ValueError):
        ExchangeOnpremConfig(ews_url=bad)


def test_plain_http_needs_the_explicit_lab_switch():
    assert client(ews_url="http://lab.internal", allow_http=True).ews_url == "http://lab.internal/EWS/Exchange.asmx"


@pytest.mark.parametrize("value", ["not-an-address", "a@b", "ok@example.com, broken"])
def test_shared_mailboxes_must_be_addresses(value):
    with pytest.raises(ValueError):
        client(shared_mailboxes=value)
    with pytest.raises(ValueError):
        ExchangeOnpremConfig(ews_url="https://mail.example.com", shared_mailboxes=value)


def test_a_connection_needs_at_least_one_mailbox():
    with pytest.raises(ValueError):
        client(shared_mailboxes="", include_own_mailbox=False)
    with pytest.raises(ValueError):
        ExchangeOnpremConfig(ews_url="https://mail.example.com", include_own_mailbox=False)


def test_ntlm_requires_both_username_and_password():
    with pytest.raises(ValueError):
        client(password="")


# ----------------------------------------------------------------------- folders

def test_folder_tree_covers_every_mailbox_and_only_mail_folders(exchange):
    paths = [f["path"] for f in client().list_mail_folders()]
    assert "Inbox" in paths and "Inbox/Projects/Apollo" in paths and "Archive 2025" in paths
    assert f"{SUPPORT}/Inbox/Escalations" in paths and f"{SALES}/Inbox" in paths
    # Calendars and contacts live in the same tree on the server but are not mail.
    assert not any("Calendar" in p or "Contacts" in p for p in paths)
    assert len(paths) == len(set(paths))


def test_folder_counts_reflect_the_mailbox(exchange):
    inbox = client().resolve_mail_folder("Inbox")
    assert (inbox["total_count"], inbox["unread_count"]) == (3, 1)


def test_a_shared_mailbox_without_access_is_skipped_and_reported(exchange):
    exchange.denied.add(SALES)
    c = client()
    paths = [f["path"] for f in c.list_mail_folders()]
    assert not any(p.startswith(SALES) for p in paths)
    assert f"{SUPPORT}/Inbox" in paths
    outcome = c.test_connection()
    assert outcome["success"] is True and SALES in outcome["message"]


def test_no_reachable_mailbox_is_an_error_not_an_empty_mailbox(exchange):
    exchange.denied.update({SUPPORT, SALES})
    c = client(include_own_mailbox=False)
    with pytest.raises(ExchangeError):
        c.list_mail_folders()
    assert c.test_connection()["success"] is False


def test_unknown_shared_mailbox_does_not_break_the_others(exchange):
    c = client(shared_mailboxes=f"{SUPPORT}, nobody@example.com")
    assert any(f["path"] == f"{SUPPORT}/Inbox" for f in c.list_mail_folders())
    assert "nobody@example.com" in c.test_connection()["message"]


def test_folder_level_sharing_exposes_the_inbox_subtree(exchange):
    """A colleague who shared only their Inbox: the top of the mailbox is not
    visible, but the Inbox and what is under it must still be reachable."""
    exchange.inbox_only.add(SUPPORT)
    c = client()
    paths = [f["path"] for f in c.list_mail_folders() if f["path"].startswith(SUPPORT)]
    assert paths == [f"{SUPPORT}/Inbox", f"{SUPPORT}/Inbox/Escalations"]
    assert subjects(c.list_messages(folder=SUPPORT)) == ["Customer cannot log in", "Outage in billing"]


def test_large_folder_trees_are_walked_page_by_page(exchange):
    for i in range(450):
        exchange.own.add_folder(f"Team {i:03d}")
    paths = [f["path"] for f in client(shared_mailboxes="").list_mail_folders()]
    assert sum(p.startswith("Team ") for p in paths) == 450


@pytest.mark.parametrize("given, expected", [
    ("Inbox/Projects/Apollo", "Inbox/Projects/Apollo"),
    ("inbox/projects/apollo", "Inbox/Projects/Apollo"),     # case-insensitive path
    ("/Inbox/Projects/", "Inbox/Projects"),                  # tolerant of slashes
    ("Apollo", "Inbox/Projects/Apollo"),                     # unique display name
    ("sent", "Sent Items"),                                  # well-known alias
    ("junk", "Junk Email"),
    (f"{SUPPORT}/Inbox/Escalations", f"{SUPPORT}/Inbox/Escalations"),
])
def test_folders_resolve_by_path_name_or_alias(exchange, given, expected):
    assert client().resolve_mail_folder(given)["path"] == expected


def test_folder_ids_resolve_to_themselves(exchange):
    c = client()
    for row in c.list_mail_folders():
        assert c.resolve_mail_folder(row["id"])["path"] == row["path"]


def test_well_known_names_work_on_a_localized_server(monkeypatch):
    """The Inbox is found by its role, not by an English display name."""
    _exchange_ews.forget_rejected_credentials()
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    server = FakeExchange()
    server.own = type(server.own)("own", localized={"inbox": "Posteingang", "sentitems": "Gesendete Elemente"})
    server.own.add("Posteingang", msg("Willkommen", 1))
    monkeypatch.setattr(requests.Session, "post",
                        lambda session, url, data=None, **kw: _Response(*server.handle(data)))
    c = client(shared_mailboxes="")
    assert c.resolve_mail_folder("inbox")["path"] == "Posteingang"
    assert subjects(c.list_messages()) == ["Willkommen"]


def test_unknown_or_ambiguous_folder_names_list_the_real_ones(exchange):
    c = client()
    with pytest.raises(ValueError) as unknown:
        c.resolve_mail_folder("No Such Folder")
    assert "Inbox/Projects/Apollo" in str(unknown.value)
    # "Inbox" exists in three mailboxes: the default one wins through its role,
    # but a plain duplicate display name must not be guessed.
    exchange.own.add_folder("Escalations", parent="Inbox")
    with pytest.raises(ValueError) as ambiguous:
        client().resolve_mail_folder("Escalations")
    assert f"{SUPPORT}/Inbox/Escalations" in str(ambiguous.value)


# ----------------------------------------------------------------------- listing

def test_listing_with_no_folder_covers_the_whole_mailbox_newest_first(exchange):
    """Rules file incoming mail into subfolders; "what came in" must include it."""
    result = client().list_messages()
    assert subjects(result) == [
        "Quarterly revenue report",   # Inbox, 1h
        "Apollo launch checklist",    # Inbox/Projects/Apollo, 3h
        "Lunch on Thursday?",         # Inbox, 5h
        "Vendor contract renewal",    # Inbox, 30h
        "Revenue forecast 2025",      # Archive 2025, 400h
    ]
    assert result["next_cursor"] is None and result["folder"] is None
    # Shared mailboxes are not mixed in, but the agent is told they exist.
    assert SUPPORT in result["note"] and SALES in result["note"]


def test_whole_mailbox_listing_leaves_out_what_did_not_arrive(exchange):
    exchange.own.add("Sent Items", msg("Re: revenue", 0.5))
    exchange.own.add("Drafts", msg("Unsent draft", 0.4))
    listed = subjects(client().list_messages())
    for left_out in ("Re: revenue", "Unsent draft", "Revenue spam offer", "You won revenue"):
        assert left_out not in listed
    assert subjects(client().list_messages(folder="sent")) == ["Re: revenue"]
    assert subjects(client().list_messages(folder="Inbox")) == [
        "Quarterly revenue report", "Lunch on Thursday?", "Vendor contract renewal",
    ]


def test_unread_mail_in_a_subfolder_is_not_missed(exchange):
    assert subjects(client().list_messages(unread_only=True)) == [
        "Quarterly revenue report", "Apollo launch checklist",
    ]


@pytest.mark.parametrize("limit", [1, 4, 25, 200])
@pytest.mark.parametrize("server_page_cap", [1000, 3])
@pytest.mark.parametrize("multi_folder", [True, False])
def test_whole_mailbox_listing_pages_newest_first_exactly_once(exchange, limit, server_page_cap, multi_folder):
    """Messages are spread unevenly over many folders, several share a
    timestamp, and the server may cap its pages or refuse several folders per
    request: the concatenated pages are still every message, newest first."""
    exchange.max_page = server_page_cap
    exchange.reject_multi_folder_search = not multi_folder
    exchange.own.by_role("inbox").messages.clear()
    for folder in list(exchange.own.folders.values()):
        folder.messages.clear()
    expected = []
    for i in range(12):
        exchange.own.add_folder(f"Team {i:02d}")
        for j in range((i * 7) % 9):
            # i + j collides across folders, so ties cross folder boundaries.
            expected.append(exchange.own.add(f"Team {i:02d}", msg(f"mail {i}-{j}", float(i + j))))
    if limit == 1 and server_page_cap == 3:
        expected = expected[:20]  # keep the slowest combination small
        for folder in exchange.own.folders.values():
            folder.messages[:] = [m for m in folder.messages if m in expected]
    items, _ = page_all(client(shared_mailboxes="").list_messages, max_results=limit)
    ids = [i["id"] for i in items]
    assert sorted(ids) == sorted(m.id for m in expected) and len(ids) == len(set(ids))
    times = [i["modified_at"] for i in items]
    assert times == sorted(times, reverse=True)


def test_whole_mailbox_cursor_belongs_to_its_mailbox(exchange):
    cursor = client().list_messages(max_results=1)["next_cursor"]
    assert cursor
    with pytest.raises(ValueError):
        client().list_messages(folder=SUPPORT, cursor=cursor)
    with pytest.raises(ValueError):
        client().list_messages(folder="Inbox", cursor=cursor)


def test_a_mailbox_with_too_many_folders_degrades_to_its_inbox_and_says_so(exchange):
    exchange.reject_multi_folder_search = True   # one folder per request
    for i in range(40):
        exchange.own.add_folder(f"Team {i:02d}")
        exchange.own.add(f"Team {i:02d}", msg(f"team mail {i}", 50 + i))
    c = client(shared_mailboxes="")
    c.search_messages("revenue")                 # the client learns the server's limit
    before = exchange.state["posts"]
    result = c.list_messages()
    assert subjects(result) == ["Quarterly revenue report", "Lunch on Thursday?", "Vendor contract renewal"]
    assert "only its Inbox" in result["note"]
    assert exchange.state["posts"] - before <= 2


def test_listing_rows_carry_what_the_agent_triages_on(exchange):
    row = client().list_messages()["items"][0]
    assert row["id"] and row["mime_type"] == "message/rfc822"
    assert row["from"] == "Dana Levi" and row["is_read"] is False
    assert row["modified_at"] == stamp(1) and row["folder"] == "Inbox"
    assert "twenty percent" in row["preview"] and row["web_url"].startswith("https://")


def test_listing_filters_are_applied_by_the_server(exchange):
    c = client()
    assert subjects(c.list_messages(folder="Inbox", unread_only=True)) == ["Quarterly revenue report"]
    window = c.list_messages(received_after=stamp(10), received_before=stamp(2))
    assert subjects(window) == ["Apollo launch checklist", "Lunch on Thursday?"]
    both = c.list_messages(unread_only=True, received_after=stamp(2))
    assert subjects(both) == ["Quarterly revenue report"]
    assert subjects(c.list_messages(received_after=stamp(0))) == []


def test_received_bounds_accept_plain_dates(exchange):
    assert subjects(client().list_messages(received_after="2026-10-05")) == [
        "Quarterly revenue report", "Apollo launch checklist", "Lunch on Thursday?",
    ]
    with pytest.raises(ValueError):
        client().list_messages(received_after="yesterday")


@pytest.mark.parametrize("total, page", [(250, 100), (101, 100), (37, 10), (100, 100)])
def test_listing_pages_to_completion_without_gaps_or_repeats(exchange, total, page):
    exchange.own.add_folder("Bulk")
    for i in range(total):
        exchange.own.add("Bulk", msg(f"Bulk {i:04d}", i / 10))
    items, _ = page_all(client().list_messages, folder="Bulk", max_results=page)
    assert [i["name"] for i in items] == [f"Bulk {i:04d}" for i in range(total)]


def test_listing_survives_a_server_that_caps_page_size(exchange):
    exchange.max_page = 7
    exchange.own.add_folder("Bulk")
    for i in range(40):
        exchange.own.add("Bulk", msg(f"Bulk {i:02d}", i))
    result = client().list_messages(folder="Bulk", max_results=25)
    assert len(result["items"]) == 25 and result["next_cursor"]
    items, _ = page_all(client().list_messages, folder="Bulk", max_results=25)
    assert len({i["id"] for i in items}) == 40


def test_a_cursor_only_continues_the_listing_it_came_from(exchange):
    exchange.own.add_folder("Bulk")
    for i in range(30):
        exchange.own.add("Bulk", msg(f"Bulk {i}", i))
    cursor = client().list_messages(folder="Bulk", max_results=10)["next_cursor"]
    with pytest.raises(ValueError):
        client().list_messages(folder="Inbox", cursor=cursor)
    with pytest.raises(ValueError):
        client().list_messages(folder="Bulk", cursor="not-a-cursor")


def test_a_shared_mailbox_is_listed_by_its_address(exchange):
    c = client()
    assert subjects(c.list_messages(folder=SUPPORT)) == ["Customer cannot log in", "Outage in billing"]
    assert subjects(c.list_messages(folder=f"{SUPPORT}/Inbox/Escalations")) == ["Outage in billing"]
    assert c.list_messages(folder=SUPPORT)["items"][0]["folder"] == f"{SUPPORT}/Inbox"


def test_service_account_can_leave_its_own_mailbox_out(exchange):
    c = client(include_own_mailbox=False)
    paths = [f["path"] for f in c.list_mail_folders()]
    # The first shared mailbox becomes the default and needs no prefix.
    assert "Inbox/Escalations" in paths and f"{SALES}/Inbox" in paths
    assert not any(p.startswith("Archive 2025") for p in paths)
    assert subjects(c.list_messages()) == ["Customer cannot log in", "Outage in billing"]
    assert subjects(c.list_messages(folder=SUPPORT)) == ["Customer cannot log in", "Outage in billing"]


# ------------------------------------------------------------------------ search

def test_search_spans_every_mailbox_newest_first(exchange):
    result = client().search_messages("revenue")
    assert subjects(result) == [
        "Quarterly revenue report",     # own Inbox, 1h
        "Customer cannot log in",       # shared mailbox, 2h (body match)
        "Revenue forecast 2025",        # own archive folder, 400h
    ]
    assert result["next_cursor"] is None and result["folder"] is None


def test_wide_search_leaves_out_deleted_and_junk_unless_named(exchange):
    c = client()
    found = subjects(c.search_messages("revenue"))
    assert "Revenue spam offer" not in found and "You won revenue" not in found
    assert subjects(c.search_messages("revenue", folder="Deleted Items")) == ["Revenue spam offer"]
    assert subjects(c.search_messages("revenue", folder="junk")) == ["You won revenue"]


def test_search_can_be_scoped_to_a_folder_or_a_whole_mailbox(exchange):
    c = client()
    assert subjects(c.search_messages("revenue", folder="Inbox")) == ["Quarterly revenue report"]
    in_support = c.search_messages("billing OR log", folder=SUPPORT)
    assert subjects(in_support) == ["Customer cannot log in", "Outage in billing"]
    assert in_support["folder"]["path"] == SUPPORT


@pytest.mark.parametrize("query, expected", [
    ("from:noa", ["Lunch on Thursday?"]),
    ("subject:contract", ["Vendor contract renewal"]),
    ('"launch readiness"', ["Apollo launch checklist"]),
    ("contract OR lunch", ["Lunch on Thursday?", "Vendor contract renewal"]),
    ("nothing-matches-this", []),
])
def test_search_passes_the_query_through_to_exchange(exchange, query, expected):
    assert subjects(client(shared_mailboxes="").search_messages(query)) == expected


def test_search_filters_narrow_the_results(exchange):
    c = client()
    assert subjects(c.search_messages("revenue", unread_only=True)) == [
        "Quarterly revenue report", "Customer cannot log in",
    ]
    assert subjects(c.search_messages("revenue", received_before=stamp(100))) == ["Revenue forecast 2025"]
    assert subjects(c.search_messages("revenue", received_after=stamp(1.5))) == ["Quarterly revenue report"]


def test_empty_search_is_refused(exchange):
    with pytest.raises(ValueError):
        client().search_messages("   ")


@pytest.mark.parametrize("limit", [3, 7, 50])
@pytest.mark.parametrize("multi_folder", [True, False])
def test_wide_search_pages_to_completion_exactly_once(exchange, limit, multi_folder):
    """Hits are spread over many folders and mailboxes; however the result is
    paged — and whether or not the server searches several folders at once —
    every hit is returned once."""
    exchange.reject_multi_folder_search = not multi_folder
    expected = set()
    for i in range(30):
        exchange.own.add_folder(f"Team {i:02d}")
        for j in range(i % 4):
            expected.add(exchange.own.add(f"Team {i:02d}", msg(f"needle {i}-{j}", i + j / 10, body="haystack")).id)
    for j in range(130):  # one folder with more hits than a page holds
        expected.add(exchange.shared[SUPPORT].add("Inbox", msg(f"needle support {j}", 500 + j)).id)
    items, _ = page_all(client().search_messages, query="needle", max_results=limit)
    ids = [i["id"] for i in items]
    assert len(ids) == len(set(ids)) and set(ids) == expected


def test_a_search_cursor_resumes_mid_folder(exchange):
    exchange.own.add_folder("Bulk")
    for i in range(45):
        exchange.own.add("Bulk", msg(f"needle {i:02d}", i))
    c = client(shared_mailboxes="")
    first = c.search_messages("needle", folder="Bulk", max_results=20)
    second = c.search_messages("needle", folder="Bulk", max_results=20, cursor=first["next_cursor"])
    assert subjects(first) == [f"needle {i:02d}" for i in range(20)]
    assert subjects(second) == [f"needle {i:02d}" for i in range(20, 40)]
    with pytest.raises(ValueError):
        c.search_messages("needle", folder="Inbox", cursor=first["next_cursor"])


def test_search_skips_empty_folders(exchange):
    for i in range(60):
        exchange.own.add_folder(f"Empty {i}")
    client(shared_mailboxes="").search_messages("revenue")
    searched = [r for r in exchange.requests if r.tag.endswith("FindItem")]
    folders_asked = sum(len(r.find(f"{{{_exchange_ews.NS_M}}}ParentFolderIds")) for r in searched)
    assert folders_asked < 10


def test_a_folder_that_vanishes_mid_search_does_not_fail_it(exchange):
    c = client()
    c.list_mail_folders()
    exchange.hidden_folders.add(exchange.own.by_path("Archive 2025").id)
    assert subjects(c.search_messages("revenue")) == ["Quarterly revenue report", "Customer cannot log in"]


def test_one_call_is_bounded_and_resumable(exchange):
    """A search across more folders than one call may visit stops with a
    cursor instead of running unbounded, and the follow-up finishes the job."""
    exchange.reject_multi_folder_search = True
    for i in range(80):
        exchange.own.add_folder(f"Team {i:02d}")
        exchange.own.add(f"Team {i:02d}", msg(f"needle {i:02d}", i))
    c = client(shared_mailboxes="")
    before = exchange.state["posts"]
    first = c.search_messages("needle", max_results=200)
    assert first["next_cursor"] and exchange.state["posts"] - before < 60
    items, _ = page_all(client(shared_mailboxes="").search_messages, query="needle", max_results=200)
    assert len({i["id"] for i in items}) == 80


# ----------------------------------------------------------------------- reading

def test_reading_renders_headers_link_and_plain_text(exchange):
    exchange.own.add("Inbox", msg("Budget approval", 0.5, sender=("Dana Levi", "dana@example.com"),
                                  body="Approved.\nSee the attached sheet.",
                                  to=[("Finance Team", "finance@example.com"), ("", "cfo@example.com")],
                                  cc=[("Noa Cohen", "noa@example.com")], attachments=["budget.xlsx", "notes.pdf"]))
    c = client()
    text = c.read_file(c.list_messages()["items"][0]["id"])
    header, body = text.split("\n\n", 1)
    assert "Subject: Budget approval" in header
    assert "From: Dana Levi <dana@example.com>" in header
    assert "To: Finance Team <finance@example.com>, cfo@example.com" in header
    assert "Cc: Noa Cohen <noa@example.com>" in header
    assert "Attachments: budget.xlsx, notes.pdf" in header
    assert "Link: https://" in header and f"Date: {stamp(0.5)}" in header
    assert body == "Approved.\nSee the attached sheet."


def test_reading_omits_lines_the_message_does_not_have(exchange):
    exchange.web_link_base = None
    c = client()
    header = c.read_file(c.list_messages()["items"][1]["id"]).split("\n\n", 1)[0]
    assert "Link:" not in header and "Cc:" not in header and "Attachments:" not in header


def test_html_only_bodies_are_reduced_to_text(exchange):
    exchange.own.add("Inbox", msg("Styled", 0.1, body="<p>Hello <b>team</b></p><p>Second&nbsp;line</p>", html=True))
    c = client()
    body = c.read_file(c.list_messages()["items"][0]["id"]).split("\n\n", 1)[1]
    assert "<" not in body and "Hello team" in body and "Second line" in body


def test_reading_a_missing_or_malformed_id_explains_itself(exchange):
    c = client()
    gone = c.list_messages()["items"][0]["id"]
    exchange.own.by_role("inbox").messages.clear()
    for bad in (gone, "not an id"):
        with pytest.raises(ExchangeError) as error:
            c.read_file(bad)
        assert error.value.code in ("ErrorItemNotFound", "ErrorInvalidIdMalformed")


def test_control_characters_in_a_message_do_not_break_the_response(exchange):
    """Exchange serves stray control characters as character references that
    XML forbids; one such byte must not make the whole mailbox unreadable."""
    exchange.own.add("Inbox", msg("Noisy", 0.2, body="before MARK after"))
    c = client()
    item_id = c.list_messages()["items"][0]["id"]
    _, clean = exchange.handle(_exchange_ews.EwsTransport._envelope(_exchange_ews.get_item(item_id)))
    exchange.state["raw"] = clean.replace(b"MARK", b"&#x3;&#31;&#x1F;")
    body = c.read_file(item_id).split("\n\n", 1)[1]
    assert body == "before  after"


def test_non_message_items_are_listed_and_readable(exchange):
    exchange.own.add("Inbox", msg("Team sync", 0.3, body="Agenda inside.", kind="MeetingRequest"))
    c = client()
    first = c.list_messages()["items"][0]
    assert first["name"] == "Team sync" and "Agenda inside." in c.read_file(first["id"])


# ------------------------------------------------------------ transport & safety

def test_only_read_operations_are_ever_sent(exchange):
    c = client()
    c.test_connection()
    c.list_mail_folders()
    items = c.list_messages()["items"]
    c.search_messages("revenue")
    c.read_file(items[0]["id"])
    assert exchange.operations and set(exchange.operations) <= READ_OPERATIONS


def test_rejected_credentials_are_not_sent_again(exchange):
    """A wrong password must cost one failed sign-in, not one per tool call:
    repeated failures lock the account in the directory."""
    exchange.state["status"] = 401
    for _ in range(4):
        with pytest.raises(ExchangeError) as error:
            client().list_messages()
        assert error.value.code == "Unauthorized"
    assert exchange.state["posts"] == 1
    assert client().test_connection()["success"] is False
    assert exchange.state["posts"] == 1


def test_corrected_credentials_are_tried_immediately(exchange):
    exchange.state["status"] = 401
    with pytest.raises(ExchangeError):
        client().list_messages()
    exchange.state["status"] = None
    assert subjects(client(password="the-new-password").list_messages())
    # A different person's sign-in on the same server is unaffected too.
    assert subjects(client(username="CORP\\noa").list_messages())


def test_rejection_is_forgotten_after_a_while(exchange, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(_exchange_ews.time, "monotonic", lambda: now[0])
    exchange.state["status"] = 401
    with pytest.raises(ExchangeError):
        client().list_messages()
    exchange.state["status"] = None
    with pytest.raises(ExchangeError):
        client().list_messages()
    now[0] += _exchange_ews.REJECTED_CREDENTIAL_TTL_SECONDS + 1
    assert subjects(client().list_messages())


def test_internal_certificate_authorities_come_from_the_deployment(exchange, monkeypatch):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/etc/ssl/corp-ca.pem")
    exchange.state["verify"] = "/etc/ssl/corp-ca.pem"
    assert subjects(client().list_messages())


def test_each_request_runs_as_the_configured_identity(exchange):
    client(username="CORP\\noa").list_messages()
    assert set(exchange.state["identities"]) == {"CORP\\noa"}


def test_a_busy_server_is_retried_once_after_its_back_off(exchange):
    exchange.busy_requests = 1
    exchange.busy_back_off_ms = 700
    assert subjects(client().list_messages())
    assert exchange.state["slept"] == [0.7]


def test_a_server_that_stays_busy_fails_without_hammering_it(exchange):
    exchange.busy_requests = 50
    exchange.busy_back_off_ms = 60_000
    with pytest.raises(ExchangeError) as error:
        client().list_messages()
    assert error.value.code == "ErrorServerBusy"
    assert exchange.state["posts"] == 2
    assert exchange.state["slept"] == [_exchange_ews.MAX_BACKOFF_SECONDS]


def test_a_dropped_connection_is_retried_once(exchange):
    exchange.state["fail_network"] = 1
    assert subjects(client().list_messages())
    exchange.state["fail_network"] = 5
    with pytest.raises(ExchangeError) as error:
        client().list_messages()
    assert error.value.code == "Unreachable"


@pytest.mark.parametrize("status, code", [(302, "Redirected"), (404, "NotFound"), (403, "Forbidden"), (502, "HttpError")])
def test_http_failures_map_to_actionable_errors(exchange, status, code):
    exchange.state["status"] = status
    with pytest.raises(ExchangeError) as error:
        client().list_messages()
    assert error.value.code == code
    # Provider response bodies never reach the message.
    assert "denied" not in str(error.value)


@pytest.mark.parametrize("payload", [b"<html><body>Sign in</body></html>", b"not xml at all", b"<a/>"])
def test_a_url_that_is_not_the_mail_service_says_so(exchange, payload):
    exchange.state["raw"] = payload
    outcome = client().test_connection()
    assert outcome["success"] is False and "not an Exchange Web Services endpoint" in outcome["message"]


def test_an_oversized_response_is_refused_before_parsing(exchange, monkeypatch):
    monkeypatch.setattr(_exchange_ews, "MAX_RESPONSE_BYTES", 2048)
    exchange.own.add("Inbox", msg("Huge", 0.1, body="x" * 10_000))
    c = client()
    with pytest.raises(ExchangeError) as error:
        c.read_file(exchange.own.by_role("inbox").messages[-1].id)
    assert error.value.code == "TooLarge"


def test_entity_expansion_in_a_response_is_refused(exchange):
    exchange.state["raw"] = (
        b'<?xml version="1.0"?><!DOCTYPE s [<!ENTITY x "boom">]>'
        b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>&x;</s:Body></s:Envelope>'
    )
    with pytest.raises(ExchangeError):
        client().list_messages()


def test_older_servers_without_listing_extras_still_list_and_search(exchange):
    exchange.reject_listing_extras = True
    c = client()
    rows = c.list_messages()["items"]
    assert [r["name"] for r in rows][:1] == ["Quarterly revenue report"]
    assert rows[0]["preview"] is None and rows[0]["web_url"] is None
    assert subjects(c.search_messages("revenue"))[0] == "Quarterly revenue report"


# ------------------------------------------------------------- connection test

def test_connection_test_reports_each_mailbox(exchange):
    outcome = client().test_connection()
    assert outcome["success"] is True
    assert SUPPORT in outcome["message"] and SALES in outcome["message"]
    assert "Not reachable" not in outcome["message"]


def test_the_generic_file_surface_maps_onto_mail(exchange):
    """Callers that predate the mail tools browse a mailbox as a file source."""
    c = client()
    assert [f["name"] for f in c.list_files(recursive=True)] == subjects(c.list_messages())
    assert [f["name"] for f in c.search_files("revenue", max_results=1)] == ["Quarterly revenue report"]
    assert c.get_schemas() == []

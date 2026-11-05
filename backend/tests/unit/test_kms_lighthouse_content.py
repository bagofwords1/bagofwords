"""KMS Lighthouse content contract (search and browse; read arrives later) against the mock KMS API.

Same boundary as test_kms_lighthouse_client.py: a real HTTP server on a real
socket (tools/kms_lighthouse/mock_kms_server.py), everything on our side real.
Behaviour is asserted on the public methods' output only.
"""
# The shared fixtures are imported from the client test module, so test
# parameters named after them look like redefinitions to the linter.
# ruff: noqa: F811
import ast
import html
import logging
import random
import re
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import pytest
import requests
import urllib3

from app.ai.tools.implementations._file_tool_common import _AUTH_ERROR_MARKERS, render_file_payload
from app.data_sources.clients import kms_lighthouse_client as kc
from app.data_sources.clients._file_source_common import FileTooLargeError, GlobScopeError, payload_name
from app.data_sources.clients.base import DataSourceClient, SearchStopped
from tests.unit.test_kms_lighthouse_client import _rows, client, kms, kms_server, token_client  # noqa: F401

_ENTRY_KEYS = {"id", "name", "path", "size", "modified_at", "mime_type", "is_folder", "kind", "status",
               "snippet", "web_url"}
_TYPED_ID = re.compile(r"^(item|scenario):[0-9]{1,12}$")


def _record(raw_id, title="Extra result", **fields):
    """One /search/full result record served through STATE["extra_results"]."""
    return {"id": str(raw_id), "subType": "T_PROCEDURE", "title": title, "description": "", **fields}


def _by_id(entries):
    return {e["id"]: e for e in entries}


def _has_control(text):
    return any(unicodedata.category(ch)[0] == "C" for ch in text)


# ------------------------------------------------------------------ mapping
@pytest.mark.parametrize("make", [client, token_client])
def test_search_maps_results_and_strips_html(kms, make):
    mock = kms["mock"]
    entries = make(kms).search_files("password billing")
    assert entries, "the search returned nothing"
    found = _by_id(entries)
    for entry in entries:
        assert set(entry) == _ENTRY_KEYS
        assert _TYPED_ID.fullmatch(entry["id"]), entry["id"]
        assert entry["is_folder"] is False and entry["mime_type"] == "text/plain"
        assert entry["size"] == 0 and entry["web_url"] is None
        assert entry["name"].endswith(".txt") and entry["path"] == entry["name"]
        for field in ("name", "path", "snippet"):
            text = entry[field]
            assert "<" not in text and "&amp;" not in text, (field, text)
            assert "eyJ" not in text and "token=" not in text and mock.URL_TOKEN not in text, (field, text)
        assert entry["modified_at"] is None or datetime.fromisoformat(entry["modified_at"])
    # Kinds from the record type; search lists no folders.
    assert found["item:279"]["kind"] == "item" and found["scenario:4400"]["kind"] == "scenario"
    assert all(e["kind"] != "folder" and not e["id"].startswith("folder:") for e in entries)
    # The highlight is gone, the words stay; the token-bearing record keeps its readable words.
    assert "password" in found["item:279"]["name"] and "reset" in found["item:279"]["snippet"]
    assert "Token notes" in found["item:4420"]["name"] and "for details" in found["item:4420"]["snippet"]
    assert "lang=en" in found["item:4420"]["snippet"]      # only the secret parameter is dropped
    # The day-first PDF date and the "yyyy-mm-dd hh:mm:ss.f" one are both ISO-8601.
    assert found["item:279"]["modified_at"].startswith("2024-04-10")
    assert found["item:301"]["modified_at"].startswith("2024-05-09T12:57:56")
    assert all(e["status"] == "online" for e in entries)


def test_search_names_are_safe_path_segments(kms):
    titles = ["Policy v2.1 / summary", "tab\there\nnewline", "..", "...", "folder:7 Archive",
              "SCENARIO:3 copy", "x" * 250, "", None, "<b>Bold</b> &amp; HTML", "lone \ud800 surrogate"]
    kms["mock"].STATE["extra_results"] = [_record(9000 + i, title) for i, title in enumerate(titles)]
    found = _by_id(client(kms).search_files("anything"))
    for i, title in enumerate(titles):
        name = found[f"item:{9000 + i}"]["name"]
        segment = name[: -len(".txt")]
        assert name.endswith(".txt") and segment, (title, name)
        assert len(segment) <= 200 and "/" not in segment and not _has_control(segment), (title, name)
        assert segment.strip(".") and segment == segment.strip(), (title, name)
        assert not re.match(r"(?i)(item|scenario|folder):", segment), (title, name)
        assert "<" not in segment, (title, name)
    # Readable words survive the clean-up.
    assert "summary" in found["item:9000"]["name"] and "Archive" in found["item:9004"]["name"]


# ------------------------------------------------------------------ status
def test_search_hides_offline_by_default(kms):
    hidden = _by_id(client(kms).search_files("refund"))
    assert "item:4410" not in hidden and hidden
    shown = _by_id(client(kms, include_offline=True).search_files("refund"))
    assert shown["item:4410"]["status"] == "offline"
    assert set(hidden) < set(shown)


@pytest.mark.parametrize("fields,online", [
    ({"status": 2}, True), ({"status": 1}, False),
    ({"status": "2"}, True), ({"status": "3"}, False),
    ({"status": "Online"}, True), ({"status": "ONLINE"}, True), ({"status": "Offline"}, False),
    # statusCode wins over status, and status_id over status.
    ({"statusCode": "2", "status": "Offline"}, True), ({"statusCode": 1, "status": "Online"}, False),
    ({"status_id": 2, "status": "Offline"}, True), ({"status_id": "5", "status": "Online"}, False),
    ({}, None),
])
def test_online_status_encodings(kms, fields, online):
    raw_id = str(uuid.uuid4().int)[:9]
    kms["mock"].STATE["extra_results"] = [_record(raw_id, **fields)]
    entry = _by_id(client(kms).search_files("x")).get(f"item:{raw_id}")
    if online is False:
        assert entry is None                     # hidden by default
        shown = _by_id(client(kms, include_offline=True).search_files("x"))[f"item:{raw_id}"]
        assert shown["status"] == "offline"
    else:
        # An unknown status is shown (the read checks it again).
        assert entry is not None and entry["status"] == ("online" if online else None)


# ------------------------------------------------------------------ paging and stops
def test_search_paging_is_bounded(kms):
    mock = kms["mock"]
    # Mostly hidden results: the client keeps paging, but never past 20 pages,
    # and says so when the last page was full (more results may exist).
    mock.STATE["extra_results"] = [_record(20000 + i, status="Offline") for i in range(3000)]
    with pytest.raises(SearchStopped) as info:
        client(kms, max_results=1000).search_files("x", limit=1000)
    # The shared tool's "it reached its result limit" (not the Max files cap, which KMS has none of).
    assert info.value.stop_reason == "max_results"
    assert mock.STATE["search_count"] == 20
    assert info.value.files and all(e["status"] != "offline" for e in info.value.files)
    # Every page asks for 50 rows; the limit trims the entries.
    mock.reset()
    mock.STATE["extra_results"] = [_record(30000 + i) for i in range(500)]
    for limit in (7, 130):
        entries = client(kms).search_files("x", limit=limit)
        assert len(entries) == limit and len({e["id"] for e in entries}) == limit
        assert mock.STATE["last_search_params"]["rows"] == "50"
    # The connection's max_results caps a larger limit.
    assert len(client(kms, max_results=60).search_files("x", limit=500)) == 60


def test_search_page_bound_with_visible_results_says_max_results(kms):
    mock = kms["mock"]
    mock.STATE["extra_results"] = [_record(50000 + i) for i in range(1200)]
    with pytest.raises(SearchStopped) as info:
        client(kms, max_results=2000).search_files("x", limit=2000)
    assert info.value.stop_reason == "max_results"
    assert mock.STATE["search_count"] == 20
    assert len(info.value.files) == 20 * 50 - 2      # the folder hit and the offline record are not listed
    # A short last page inside the bound is a complete answer, not a stop.
    mock.reset()
    mock.STATE["extra_results"] = [_record(60000 + i) for i in range(120)]
    entries = client(kms, max_results=2000).search_files("x", limit=2000)
    assert len(entries) == 120 + 5 and mock.STATE["search_count"] == 3


def test_search_dedupes_by_id(kms):
    kms["mock"].STATE["extra_results"] = [_record(4242, "Same"), _record(4242, "Same again")]
    ids = [e["id"] for e in client(kms).search_files("x")]
    assert ids.count("item:4242") == 1


def test_search_honors_deadline(kms):
    mock = kms["mock"]
    with pytest.raises(SearchStopped) as info:
        client(kms).search_files("billing", deadline=time.monotonic() - 1)
    assert info.value.stop_reason == "deadline" and info.value.files == []
    assert mock.STATE["search_count"] == 0


def test_search_timeout_at_the_deadline_keeps_the_earlier_pages(kms):
    mock = kms["mock"]
    mock.STATE["extra_results"] = [_record(80000 + i) for i in range(120)]
    mock.STATE["hold_search_after_first"] = True     # page 2 answers only after the client gave up
    try:
        with pytest.raises(SearchStopped) as info:
            token_client(kms).search_files("x", deadline=time.monotonic() + 2)
    finally:
        mock.HELD["release"].set()
    assert mock.HELD["arrived"].is_set()             # page 2 was sent and timed out at the deadline
    assert info.value.stop_reason == "deadline"
    # Page 1's 50 rows minus the folder hit and the offline record.
    assert len(info.value.files) == 48 and all(_TYPED_ID.fullmatch(e["id"]) for e in info.value.files)


def test_search_timeout_with_time_left_is_unreachable(kms, monkeypatch):
    mock = kms["mock"]
    mock.STATE["extra_results"] = [_record(82000 + i) for i in range(120)]
    mock.STATE["hold_search_after_first"] = True
    monkeypatch.setattr(kc, "_DEFAULT_TIMEOUT", (1, 1))    # KMS is slow, and the deadline is far away
    try:
        with pytest.raises(kc.KmsHTTPError) as info:
            token_client(kms).search_files("x", deadline=time.monotonic() + 30)
    finally:
        mock.HELD["release"].set()
    assert mock.HELD["arrived"].is_set()
    assert info.value.kind == "unreachable" and not isinstance(info.value, SearchStopped)
    assert info.value.__context__ is None and info.value.__cause__ is None


def test_search_honors_cancel_check(kms):
    mock = kms["mock"]
    mock.STATE["extra_results"] = [_record(40000 + i) for i in range(300)]
    checks = []

    def cancel_check():
        checks.append(1)
        return len(checks) > 1                   # true from the second page on

    with pytest.raises(SearchStopped) as info:
        client(kms).search_files("x", cancel_check=cancel_check)
    assert info.value.stop_reason == "cancelled"
    assert mock.STATE["search_count"] == 1
    # The first page's files, and nothing from beyond it.
    first_page = int(mock.STATE["last_search_params"]["rows"])
    files = info.value.files
    assert files and len(files) <= first_page
    assert all(_TYPED_ID.fullmatch(e["id"]) for e in files)
    extras = [int(e["id"].split(":")[1]) for e in files if int(e["id"].split(":")[1]) >= 40000]
    assert extras and max(extras) < 40000 + first_page


def test_search_400_names_both_causes(kms):
    mock = kms["mock"]
    mock.STATE["search_400"] = True
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms).search_files("a very long question")
    err = info.value
    assert err.kind == "bad_request"
    assert "too long" in str(err) and "Layout ID" in str(err)
    assert err.__context__ is None and err.__cause__ is None
    assert "a very long question" not in str(err)


@pytest.mark.parametrize("login_status", [422, 409])
def test_search_login_4xx_is_not_blamed_on_the_query(kms, login_status):
    mock = kms["mock"]
    mock.STATE["login_status"] = login_status
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms).search_files("billing")
    err = info.value
    assert str(err) != kc._SEARCH_REJECTED and "too long" not in str(err)
    assert err.status == login_status
    assert err.__context__ is None and err.__cause__ is None
    assert mock.STATE["login_count"] == 1 and mock.STATE["search_count"] == 0


@pytest.mark.parametrize("query", ["", "   ", "\n\t", None, "\x00\x1f"])
def test_empty_query_makes_no_request(kms, query):
    assert client(kms).search_files(query) == []
    assert token_client(kms).search_files(query) == []
    assert kms["mock"].REQUEST_LOG == []


@pytest.mark.parametrize("length", [201, 500, 5000])
def test_long_query_is_capped_not_rejected(kms, length):
    mock = kms["mock"]
    words = ("refund policy " * (length // 14 + 1))[:length]
    assert client(kms).search_files(words)
    sent = mock.STATE["last_search_params"]["query"]
    assert sent and words.startswith(sent) and len(sent) <= kc._MAX_QUERY_CHARS
    # Control characters never reach KMS.
    client(kms).search_files("refund\x00 po\x1blicy\x7f\n" + words)
    sent = mock.STATE["last_search_params"]["query"]
    assert not _has_control(sent) and "refund" in sent and len(sent) <= kc._MAX_QUERY_CHARS


@pytest.mark.parametrize("layout_id", [None, 7, 31])
def test_search_sends_layout_id_only_when_set(kms, layout_id):
    client(kms, layout_id=layout_id).search_files("billing")
    params = kms["mock"].STATE["last_search_params"]
    assert params["layoutId"] == (None if layout_id is None else str(layout_id))
    assert params["query"] == "billing"


@pytest.mark.parametrize("make", [client, token_client])
def test_search_runs_as_the_identity(kms, make):
    alice = _by_id(make(kms, user="alice").search_files("escalation"))
    bob = _by_id(make(kms, user="bob").search_files("escalation"))
    assert "item:4430" in alice and "item:4430" not in bob
    assert "item:301" in bob
    searches = [row for row in kms["mock"].REQUEST_LOG if row[2].endswith("/search/full")]
    assert [row[0] for row in searches] == ["alice", "bob"]


# ------------------------------------------------------------------ browse
_TREE_PATH = "/cm/tree/branch/view"
_FOLDER_ID = re.compile(r"^folder:[0-9]{1,12}$")
_HOSTILE_ITEMS = {f"item:{int(folder_id) + 300}" for folder_id in ("20", "21", "22", "23", "24")}


def _folder_part(entry):
    return entry["path"].rsplit("/", 1)[0]


def _ids(entries):
    return {e["id"] for e in entries}


@pytest.mark.parametrize("make", [client, token_client])
def test_list_walks_tree_from_root(kms, make):
    entries = make(kms).list_files()
    found = _by_id(entries)
    assert len(found) == len(entries)
    for entry in entries:
        assert set(entry) == _ENTRY_KEYS, entry
        if entry["is_folder"]:
            assert _FOLDER_ID.fullmatch(entry["id"]) and entry["kind"] == "folder", entry
        else:
            assert _TYPED_ID.fullmatch(entry["id"]), entry
            assert entry["mime_type"] == "text/plain" and entry["size"] == 0 and entry["web_url"] is None
    assert found["folder:10"]["path"] == "Procedures" and found["folder:10"]["is_folder"] is True
    assert found["folder:12"]["path"] == "Procedures/Billing"
    assert found["item:279"]["path"] == "Accounts/Reset a customer password.txt"
    assert found["item:302"]["path"] == "Procedures/Billing/Refund a payment.txt"
    assert found["scenario:4401"]["path"] == "Procedures/Billing/Card blocked flow.txt"
    assert found["scenario:4401"]["kind"] == "scenario" and found["item:302"]["kind"] == "item"
    assert found["item:301"]["modified_at"] == "2024-04-10"
    # With a root, paths start below it and nothing outside it is listed.
    rooted = _by_id(make(kms, root_folder_id=10).list_files())
    assert rooted["item:302"]["path"] == "Billing/Refund a payment.txt"
    assert rooted["item:301"]["path"] == "Billing dispute.txt"
    assert "item:279" not in rooted and "folder:10" not in rooted


def test_entry_name_is_last_path_segment(kms):
    files = [e for e in client(kms).list_files() if not e["is_folder"]]
    assert files
    for entry in files:
        assert entry["name"] == entry["path"].rsplit("/", 1)[-1], entry
        assert entry["name"].endswith(".txt"), entry
    # A '/' in a title does not add a path level.
    policy = _by_id(files)["item:305"]
    assert policy["path"].count("/") == 1 and "summary" in policy["name"], policy


def test_list_one_folder(kms):
    one_level = {"item:301", "item:305", "folder:12", "folder:13", "folder:14"}
    assert _ids(client(kms).list_files(folder_id="folder:10", recursive=False)) == one_level
    assert _ids(client(kms).list_files(folder_id="folder:10")) == one_level        # a folder lists one level
    deep = _ids(client(kms).list_files(folder_id="folder:10", recursive=True))
    assert deep > one_level and {"item:302", "scenario:4401", "item:303", "item:304"} <= deep


def test_list_folder_by_path(kms):
    by_path = client(kms).list_files(folder_id="Procedures/Billing", recursive=True)
    by_id = client(kms).list_files(folder_id="folder:12", recursive=True)
    assert _ids(by_path) == _ids(by_id)
    assert _ids(by_path) == {"item:302", "scenario:4401"}
    # Paths stay full paths from the root, so they resolve again.
    assert _by_id(by_path)["item:302"]["path"] == "Procedures/Billing/Refund a payment.txt"


@pytest.mark.parametrize("root", [None, 10])
def test_entry_paths_resolve_back_to_their_folder(kms, root):
    entries = client(kms, root_folder_id=root).list_files()
    nested = [e for e in entries if "/" in e["path"]]
    assert nested
    if root is None:
        assert _ids(entries) >= _HOSTILE_ITEMS       # the hostile folder titles are walked
    by_folder = {}
    for entry in nested:
        by_folder.setdefault(_folder_part(entry), set()).add(entry["id"])
    for folder, ids in by_folder.items():
        for segment in folder.split("/"):
            assert segment and len(segment) <= 200 and not _has_control(segment), (folder, segment)
        listed = client(kms, root_folder_id=root).list_files(folder_id=folder, recursive=True)
        assert ids <= _ids(listed), (folder, ids - _ids(listed))


def test_sibling_folders_with_same_title_get_distinct_paths(kms):
    found = _by_id(client(kms).list_files())
    first, second = found["folder:13"]["path"], found["folder:14"]["path"]
    assert first != second and first.startswith("Procedures/Forms") and second.startswith("Procedures/Forms")
    for folder, item in (("folder:13", "item:303"), ("folder:14", "item:304")):
        assert _ids(client(kms).list_files(folder_id=found[folder]["path"])) == {item}


@pytest.mark.parametrize("path", ["Nope", "Procedures/Nope", "Procedures/Billing/Refund a payment.txt",
                                  "procedures", "Forms"])
def test_unknown_folder_path_is_not_found(kms, path):
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms).list_files(folder_id=path)
    assert info.value.kind == "not_found"
    assert path not in str(info.value)


def test_list_hides_offline(kms):
    hidden = _by_id(client(kms).list_files())
    assert "item:306" not in hidden and "item:301" in hidden
    shown = _by_id(client(kms, include_offline=True).list_files())
    assert shown["item:306"]["status"] == "offline" and shown["item:301"]["status"] == "online"
    # Compared on Procedures: the full walk is capped, and the offline folder takes one of its calls.
    hidden_here = _ids(client(kms).list_files(folder_id="folder:10", recursive=True))
    shown_here = _ids(client(kms, include_offline=True).list_files(folder_id="folder:10", recursive=True))
    assert hidden_here < shown_here and shown_here - hidden_here == {"item:306", "folder:15", "item:308"}


def test_list_walk_is_bounded(kms):
    mock = kms["mock"]
    # The wide folder alone has more subfolders than the cap.
    entries = client(kms).list_files()
    assert len(_rows(kms, _TREE_PATH)) == 40 and len(entries) <= 200
    assert {f"folder:{1000 + n}" for n in range(mock.WIDE_CHILDREN)} <= _ids(entries)
    assert not {f"item:{2000 + n}" for n in range(mock.WIDE_CHILDREN)} <= _ids(entries)
    # Resolving a path counts against the same cap.
    mock.REQUEST_LOG.clear()
    client(kms).list_files(folder_id="Wide", recursive=True)
    assert len(_rows(kms, _TREE_PATH)) == 40
    assert len(client(kms).list_files(limit=7)) == 7
    assert len(client(kms, max_results=5).list_files(limit=50)) == 5


def test_folder_outside_root_is_refused(kms):
    rooted = client(kms, root_folder_id=10)
    with pytest.raises(GlobScopeError):
        rooted.list_files(folder_id="folder:11")
    assert _ids(client(kms, root_folder_id=10).list_files(folder_id="folder:12")) == {"item:302", "scenario:4401"}
    assert "item:301" in _ids(client(kms, root_folder_id=10).list_files(folder_id="folder:10"))


@pytest.mark.parametrize("make", [client, token_client])
def test_list_runs_as_the_identity(kms, make):
    alice = _ids(make(kms, user="alice").list_files())
    bob = _ids(make(kms, user="bob").list_files())
    assert {"folder:11", "item:307"} <= alice
    assert not {"folder:11", "item:307", "item:279"} & bob and {"item:301", "item:302"} <= bob
    callers = [row[0] for row in _rows(kms, _TREE_PATH)]
    assert set(callers) == {"alice", "bob"} and callers == sorted(callers)


@pytest.mark.parametrize("folder_id", [
    "folder:../1", "folder:1;x", "item:%2e%2e", "folder:" + "1" * 13, "Procedures/../Restricted",
    "folder:10\n", "FOLDER:10\n", "item:279\n", "item:279", "scenario:4401", "folder:", "folder:-1",
    "./Procedures", "Procedures/.", "Procedures//Billing", "/Procedures", "Procedures\tBilling",
    "Procedures/‮Billing", "x" * 201, "/".join(["a"] * 26), "Procedures/item:7",
])
def test_ids_are_validated_before_any_request(kms, folder_id):
    mock = kms["mock"]
    with pytest.raises(kc.KmsError) as info:
        client(kms).list_files(folder_id=folder_id)
    assert mock.REQUEST_LOG == []
    assert "%2e" not in str(info.value) and "\n" not in str(info.value) and "Restricted" not in str(info.value)
    assert info.value.__context__ is None and info.value.__cause__ is None


def test_folder_id_without_root_has_paths_from_the_top_that_resolve_back(kms):
    entries = client(kms).list_files(folder_id="folder:10", recursive=True)
    found = _by_id(entries)
    assert found["folder:12"]["path"] == "Procedures/Billing"
    assert found["item:302"]["path"] == "Procedures/Billing/Refund a payment.txt"
    assert found["item:301"]["path"] == "Procedures/Billing dispute.txt"
    for entry in entries:
        listed = client(kms).list_files(folder_id=_folder_part(entry), recursive=True)
        assert entry["id"] in _ids(listed), entry


def test_unknown_folder_id_without_root_is_not_found(kms):
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms).list_files(folder_id="folder:99999")
    assert info.value.kind == "not_found" and "99999" not in str(info.value)


def test_unknown_folder_id_in_a_small_tree_is_not_found_not_the_browse_limit(kms, monkeypatch):
    mock = kms["mock"]
    # A tree the walk lists completely in a few calls: the folder is simply not there.
    small = {key: mock.TREE[key] for key in ("10", "12", "301", "302")}
    monkeypatch.setattr(mock, "TREE", small)
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms).list_files(folder_id="folder:99999")
    assert info.value.kind == "not_found" and str(info.value) == kc._FOLDER_NOT_FOUND
    assert "browse limit" not in str(info.value)
    assert len(_rows(kms, _TREE_PATH)) <= 3


@pytest.mark.parametrize("root", [None, 10])
def test_online_folder_under_an_offline_folder_is_not_found_not_outside_root(kms, monkeypatch, root):
    mock = kms["mock"]
    # Root 10 -> offline 15 -> online 16: 16 is inside the root, only under a hidden folder.
    monkeypatch.setitem(mock.TREE, "16", {"parent": mock.OFFLINE_FOLDER, "title": "Current forms",
                                          "type": "FOLDER", "status": "2"})
    monkeypatch.setitem(mock.TREE, "309", {"parent": "16", "title": "Current form", "type": "ITEM", "status": "2"})
    monkeypatch.setitem(mock.USERS["alice"], "visible", mock.USERS["alice"]["visible"] | {"16", "309"})
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms, root_folder_id=root).list_files(folder_id="folder:16")
    # The walk opens the offline folder too, after the shown ones and within the call cap, so from the root
    # it finds 16 there and names it. From the top level, the shown folders (the wide one among them) use
    # up the cap first: the browse limit, still not found and never "outside the root".
    assert info.value.kind == "not_found"
    assert str(info.value) == (kc._FOLDER_NOT_FOUND if root is not None else kc._BEYOND_BROWSE_LIMIT)
    shown = client(kms, root_folder_id=root, include_offline=True).list_files(folder_id="folder:16")
    assert _ids(shown) == {"item:309"}
    # A folder that really is outside the root is still refused.
    if root is not None:
        with pytest.raises(GlobScopeError):
            client(kms, root_folder_id=root).list_files(folder_id="folder:11")


@pytest.mark.parametrize("root", [None, 30])
@pytest.mark.parametrize("folder", ["folder:5001", "folder:5002"])
def test_folder_found_only_past_the_call_cap_is_named(kms, root, folder):
    mock = kms["mock"]
    # From the root "30", finding 5001 takes all 40 calls (none is left to list it); 5002 needs one more.
    # From the top level, neither is reached within the cap.
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms, root_folder_id=root).list_files(folder_id=folder)
    assert info.value.kind == "not_found" and "browse limit" in str(info.value)
    if root == 30 and folder == "folder:5001":
        # Found with all 40 calls used: no call is left to list it.
        assert str(info.value).startswith("Folder found, but beyond the browse limit")
    else:
        # Not reached: the walk cannot tell a missing folder from one past the cap, so either cause is named.
        assert "not found, or beyond the browse limit" in str(info.value)
    assert len(_rows(kms, _TREE_PATH)) <= 40
    # Its folder path still lists it, in a few calls.
    mock.REQUEST_LOG.clear()
    deep = mock.DEEP_AT_CAP if folder.endswith(mock.DEEP_AT_CAP) else mock.DEEP_PAST_CAP
    wide = "Wide 38" if deep == mock.DEEP_AT_CAP else "Wide 39"
    path = f"{wide}/Deep {deep}" if root == 30 else f"Wide/{wide}/Deep {deep}"
    assert _ids(client(kms, root_folder_id=root).list_files(folder_id=path)) == {f"item:{int(deep) + 100}"}


@pytest.mark.parametrize("root", [None, 10])
def test_offline_folder_by_id_is_not_found(kms, root):
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms, root_folder_id=root).list_files(folder_id="folder:15")
    assert info.value.kind == "not_found"
    shown = client(kms, root_folder_id=root, include_offline=True).list_files(folder_id="folder:15")
    assert _ids(shown) == {"item:308"}


def _archive_tree(mock, monkeypatch, archive_children=45):
    """Folder 39 holds, in this order: offline "Archive" (40) with ``archive_children`` subfolders, online
    "Docs" (41) -> "Team" (42) -> "Target" (43, one item), and offline "Retired" (45) -> "Retired forms" (46).
    Folder 38 sits next to 39 at the top level."""
    def node(parent, title, kind="FOLDER", status="2"):
        return {"parent": parent, "title": title, "type": kind, "status": status}
    tree = {"38": node(None, "Elsewhere"), "39": node(None, "Root"),
            "40": node("39", "Archive", status="1"), "41": node("39", "Docs"), "42": node("41", "Team"),
            "43": node("42", "Target"), "44": node("43", "Target page", "ITEM"),
            "45": node("39", "Retired", status="1"), "46": node("45", "Retired forms")}
    for n in range(archive_children):
        tree[str(600 + n)] = node("40", f"Old {n}")
    monkeypatch.setattr(mock, "TREE", tree)
    monkeypatch.setitem(mock.USERS["alice"], "visible", set(tree))


def test_folder_id_lookup_opens_shown_folders_before_hidden_ones(kms, monkeypatch):
    mock = kms["mock"]
    # A hidden (offline) folder listed first, with more subfolders than the call cap, does not use up the
    # calls the target's shown ancestors need: the root level, Docs, Team, then Target itself.
    _archive_tree(mock, monkeypatch)
    listed = client(kms, root_folder_id=39).list_files(folder_id="folder:43")
    assert _ids(listed) == {"item:44"} and listed[0]["path"] == "Docs/Team/Target/Target page.txt"
    assert len(_rows(kms, _TREE_PATH)) == 4
    # A folder under a hidden one is still found (after the shown ones) and named not found.
    mock.REQUEST_LOG.clear()
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms, root_folder_id=39).list_files(folder_id="folder:600")
    assert info.value.kind == "not_found" and str(info.value) == kc._FOLDER_NOT_FOUND
    assert len(_rows(kms, _TREE_PATH)) == 5           # 39, 41, 42, 43, then 40
    assert _ids(client(kms, root_folder_id=39, include_offline=True).list_files(folder_id="folder:600")) == set()
    # Out of calls among the hidden folders: the browse limit, never "outside the root".
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms, root_folder_id=39).list_files(folder_id="folder:38")
    assert info.value.kind == "not_found" and str(info.value) == kc._BEYOND_BROWSE_LIMIT
    # A walk that completes without it is still "outside the root".
    _archive_tree(mock, monkeypatch, archive_children=3)
    with pytest.raises(GlobScopeError):
        client(kms, root_folder_id=39).list_files(folder_id="folder:38")


@pytest.mark.parametrize("status", [403, 500])
def test_a_hidden_branch_that_fails_to_list_does_not_break_a_lookup(kms, monkeypatch, status):
    mock = kms["mock"]
    _archive_tree(mock, monkeypatch, archive_children=3)
    mock.STATE["tree_status"] = {"40": status}
    rooted = client(kms, root_folder_id=39)
    assert _ids(rooted.list_files(folder_id="folder:43")) == {"item:44"}
    # The failing hidden branch is unseen: the rest of the walk still runs and finds 46 under hidden 45.
    with pytest.raises(kc.KmsHTTPError) as info:
        rooted.list_files(folder_id="folder:46")
    assert info.value.kind == "not_found" and str(info.value) == kc._FOLDER_NOT_FOUND
    assert "40" in mock.STATE["tree_status_sent"]          # the injected failure really was answered
    # A walk that skipped a branch cannot tell "outside the root" from "in the unseen branch": not found.
    with pytest.raises(kc.KmsHTTPError) as info:
        rooted.list_files(folder_id="folder:38")
    assert info.value.kind == "not_found" and str(info.value) == kc._FOLDER_NOT_FOUND
    assert info.value.__context__ is None and info.value.__cause__ is None
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms).list_files(folder_id="folder:99999")
    assert str(info.value) == kc._FOLDER_NOT_FOUND
    assert info.value.__context__ is None and info.value.__cause__ is None
    # A shown folder that fails to list still fails the lookup, and so does a refused session anywhere.
    mock.STATE["tree_status"] = {"42": status}
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms, root_folder_id=39).list_files(folder_id="folder:43")
    assert info.value.kind != "not_found"
    mock.STATE["tree_status"] = {"40": 401}
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms, root_folder_id=39).list_files(folder_id="folder:38")
    assert info.value.kind == "auth"


_SKIPPED_KIND = {400: "bad_request", 403: "forbidden", 404: "not_found", 500: "server"}


@pytest.mark.parametrize("folder", ["folder:46", "folder:600", "folder:38"])
@pytest.mark.parametrize("status", sorted(_SKIPPED_KIND))
def test_a_lookup_that_skipped_a_hidden_branch_is_never_outside_root(kms, monkeypatch, caplog, status, folder):
    mock = kms["mock"]
    # Both hidden folders under root 39 (40 holds 600, 45 holds 46) fail to list: the walk sees neither
    # branch, so it cannot know the folder is not in one of them.
    _archive_tree(mock, monkeypatch, archive_children=3)
    mock.STATE["tree_status"] = {"40": status, "45": status}
    caplog.set_level(logging.DEBUG, logger=kc.__name__)
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms, root_folder_id=39).list_files(folder_id=folder)
    assert info.value.kind == "not_found" and str(info.value) == kc._FOLDER_NOT_FOUND
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert sorted(mock.STATE["tree_status_sent"]) == ["40", "45"]
    # The debug line names the error kind only: no id, no title, no URL.
    skipped = [r.getMessage() for r in caplog.records if r.name == kc.__name__ and "skipped" in r.getMessage()]
    assert len(skipped) == 2, skipped
    for line in skipped:
        assert line.endswith(": " + _SKIPPED_KIND[status]), line
        assert not re.search(r"[0-9]|http|parentId|Archive|Retired|Root|Elsewhere", line), line
    # With nothing failing, the same walk completes and a folder outside the root is refused.
    mock.STATE["tree_status"] = {}
    with pytest.raises(GlobScopeError) as refused:
        client(kms, root_folder_id=39).list_files(folder_id="folder:38")
    assert refused.value.__context__ is None and refused.value.__cause__ is None


@pytest.mark.parametrize("folder", ["folder:46", "folder:600", "folder:38"])
@pytest.mark.parametrize("make,status,kind", [
    (client, 429, "throttled"), (token_client, 429, "throttled"), (token_client, 302, "redirect"),
])
def test_a_hidden_branch_failure_about_the_service_fails_the_lookup(kms, monkeypatch, folder, make, status, kind):
    mock = kms["mock"]
    # Throttling and a redirect say nothing about the branch: the lookup fails at the first one.
    _archive_tree(mock, monkeypatch, archive_children=3)
    mock.STATE["tree_status"] = {"40": status, "45": status}
    with pytest.raises(kc.KmsHTTPError) as info:
        make(kms, root_folder_id=39).list_files(folder_id=folder)
    assert info.value.kind == kind
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert mock.STATE["tree_status_sent"] == ["40"]


@pytest.mark.parametrize("folder", ["folder:46", "folder:600", "folder:38"])
def test_an_unreachable_hidden_branch_fails_the_lookup_at_once(kms, monkeypatch, folder):
    _archive_tree(kms["mock"], monkeypatch, archive_children=3)
    original = requests.Session.request
    failed = []

    def unreachable(self, method, url, *args, **kwargs):
        parent = (kwargs.get("params") or {}).get("parentId")
        if url.endswith(_TREE_PATH) and parent in ("40", "45"):
            failed.append(parent)
            raise requests.ConnectionError("refused")
        return original(self, method, url, *args, **kwargs)

    monkeypatch.setattr(requests.Session, "request", unreachable)
    with pytest.raises(kc.KmsHTTPError) as info:
        token_client(kms, root_folder_id=39).list_files(folder_id=folder)
    assert info.value.kind == "unreachable"
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert failed == ["40"]            # raised on the first failed attempt; hidden 45 is never tried


@pytest.mark.parametrize("folder", ["folder:46", "folder:600", "folder:38"])
@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_hidden_branch_gateway_failure_fails_the_lookup(kms, monkeypatch, folder, status):
    mock = kms["mock"]
    # A gateway answer says the service is down, not that the branch is refused: the lookup fails at once
    # (a plain 500 stays branch-specific: test_a_lookup_that_skipped_a_hidden_branch_is_never_outside_root).
    _archive_tree(mock, monkeypatch, archive_children=3)
    mock.STATE["tree_status"] = {"40": status, "45": status}
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms, root_folder_id=39).list_files(folder_id=folder)
    assert info.value.kind == "server" and info.value.status == status
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert mock.STATE["tree_status_sent"] == ["40"]


@pytest.mark.parametrize("folder", ["folder:46", "folder:38"])
@pytest.mark.parametrize("login_status", [500, 503, 200])
def test_a_login_failure_inside_a_lookup_fails_it_with_one_login(kms, monkeypatch, folder, login_status):
    mock = kms["mock"]
    # The session expires at the first hidden branch (40), and the re-login fails: a 5xx, or a 200 whose
    # body is not JSON. That is a login outage, not a branch the server refused to list.
    _archive_tree(mock, monkeypatch, archive_children=3)
    client(kms, root_folder_id=39).list_files(folder_id="folder:43")     # the shown phase; the session is pooled
    logins = mock.STATE["login_count"]
    mock.STATE["expire_sessions_at"] = "40"
    mock.STATE["login_status"] = login_status
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms, root_folder_id=39).list_files(folder_id=folder)
    assert info.value.kind == "server" and str(info.value) == kc._MESSAGES["server"]
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert mock.STATE["login_count"] == logins + 1     # no second password POST for the next hidden branch


@pytest.mark.parametrize("bad", [
    {40: 403}, {"40": "403"}, {"40": True}, {"40": 99}, {"40": 600}, {"40": 403.0}, {None: 403}, {"4x": 403},
    {"": 403}, {"40\n": 403}, {"٤٠": 403}, [("40", 403)], None, {"40": 100}, {"40": 199},
])
def test_mock_tree_status_takes_only_folder_ids_and_http_statuses(kms, bad):
    mock = kms["mock"]
    with pytest.raises(ValueError):
        mock.STATE["tree_status"] = bad
    mock.STATE["tree_status"] = {"40": 200, "45": 599}
    assert mock.STATE["tree_status"] == {"40": 200, "45": 599}


@pytest.mark.parametrize("key,bad", [
    ("login_status", 100), ("login_status", 199), ("login_status", 600), ("login_status", True),
    ("login_status", "503"), ("login_status", 503.0),
    ("ai_status", 100), ("ai_status", 199), ("ai_status", 600), ("ai_status", True), ("ai_status", "503"),
    ("ai_status", "Empty"), ("ai_status", "json"), ("ai_status", False), ("ai_status", 404.0),
])
def test_mock_forced_statuses_take_only_final_http_statuses(kms, key, bad):
    mock = kms["mock"]
    with pytest.raises(ValueError):
        mock.STATE[key] = bad
    for good in (None, 200, 599):
        mock.STATE[key] = good
        assert mock.STATE[key] == good
    for good in ("empty", "not_json", "error"):
        mock.STATE["ai_status"] = good
        assert mock.STATE["ai_status"] == good


def test_a_folder_found_with_no_call_left_to_list_it_says_so(kms):
    # From the root "30", finding 5001 takes all 40 calls, so none is left to list it.
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms, root_folder_id=30).list_files(folder_id="folder:5001")
    assert info.value.kind == "not_found"
    assert str(info.value) == ("Folder found, but beyond the browse limit; pass its folder path "
                               "(the folder part of an entry's path) instead.")
    assert len(_rows(kms, _TREE_PATH)) == 40


def test_folder_id_digits_are_compared_as_numbers(kms):
    rooted = client(kms, root_folder_id=10)
    assert _ids(rooted.list_files(folder_id="folder:010")) == _ids(rooted.list_files(folder_id="folder:10"))
    assert _ids(rooted.list_files(folder_id="folder:0012")) == {"item:302", "scenario:4401"}


# 0 is refused like the shared list_files tool refuses it (limit >= 1), as an int and as text alike;
# a fractional limit is refused, not truncated.
@pytest.mark.parametrize("limit", [-5, -1, "abc", 0, "0", 2.5, "2.5", 0.5, True, False])
def test_bad_list_limit_is_named_before_any_request(kms, limit):
    mock = kms["mock"]
    with pytest.raises(kc.KmsError) as info:
        client(kms).list_files(limit=limit)
    assert "list limit" in str(info.value) and mock.REQUEST_LOG == []
    assert info.value.__context__ is None and info.value.__cause__ is None


@pytest.mark.parametrize("limit,count", [(None, 5), (3, 3), ("3", 3), (3.0, 3), (" 4 ", 4), (50, 5)])
def test_whole_number_list_limits_are_accepted(kms, limit, count):
    assert len(client(kms, max_results=5).list_files(limit=limit)) == count


@pytest.mark.parametrize("value", ["ſcenario:5", "ıtem:5", "İtem:5", "ſcenario:4401", "FOLDER:ſ"])
def test_parse_id_refuses_unicode_case_folded_prefixes(value):
    # P5 reads by these ids: a kind outside item/scenario/folder must never come back.
    with pytest.raises(kc.KmsError) as info:
        kc._parse_id(value)
    assert str(info.value) == kc._BAD_ID


def test_parse_id_accepts_any_ascii_case():
    assert kc._parse_id("SCENARIO:5") == ("scenario", "5")
    assert kc._parse_id("Folder:7") == ("folder", "7")
    assert kc._parse_id("279") == ("item", "279")


# ------------------------------------------------------------------ read article
_FOLDER_READ = "That id is a folder; use list_files to see its contents."


def _sections(text):
    """Section heading ("## <tab>") -> the text under it, in order."""
    sections, current = {}, None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:]
            sections[current] = ""
        elif current is not None:
            sections[current] += line + "\n"
    return sections


def _scenario_rows(kms):
    return [row for row in kms["mock"].REQUEST_LOG if "/public/scenario/" in row[2]]


def test_read_article_prefers_ai_text(kms):
    mock = kms["mock"]
    text = client(kms).read_file("item:279")
    assert mock.STATE["ai_requests"] == [{"itemId": "279", "version": "2", "language": "en"}]
    lines = text.splitlines()
    assert lines[0] == "Title: Reset a customer password [redacted]"
    assert {"Kind: article (T_PROCEDURE)", "Version: 2", "Language: en", "Updated: 2024-04-10"} <= set(lines)
    assert "Summary: How to reset a password from the agent desk. Ref [redacted]" in lines
    sections = _sections(text)
    assert list(sections) == ["Overview", "Processes"]
    assert "### Summary\nAI summary: verify the caller, then reset." in sections["Overview"]
    assert "### Links\nAI links." in sections["Processes"]
    assert "Item summary" not in text and "Open the agent desk." not in text     # the item HTML was not used


def test_read_article_falls_back_to_item_html(kms):
    mock = kms["mock"]
    mock.STATE["ai_text_available"] = False
    text = client(kms).read_file("item:279")
    assert [r["itemId"] for r in mock.STATE["ai_requests"]] == ["279"]       # asked, answered 404
    assert "AI summary" not in text and "<" not in text
    assert text.splitlines()[0] == "Title: Reset a customer password [redacted]"
    sections = _sections(text)
    # String tabIds matched to the integer tabs[].id; the null and -1 elements go to "General", first.
    assert list(sections) == ["General", "Overview", "Processes"]
    assert "### Notes\nUntabbed note for every agent." in sections["General"]
    assert "### Legacy\nLegacy note on no tab." in sections["General"]
    assert "### Summary\nItem summary: verify the caller first." in sections["Overview"]
    assert "Open the agent desk.\nChoose Reset." in sections["Processes"]
    # The ITEM_IMAGE_FIELD element is skipped.
    assert "b144e1e2" not in text and "Item image" not in text


def test_ai_text_404_affects_only_that_read(kms):
    mock = kms["mock"]
    mock.STATE["ai_missing_items"] = ["279"]
    c = client(kms)
    first = c.read_file("item:279")
    assert "Item summary: verify the caller first." in first and "AI summary" not in first
    second = c.read_file("item:301")
    assert "AI text for a billing dispute." in second and "Item text" not in second
    # The 404 is not remembered: the same item asks for ai text again.
    mock.STATE["ai_missing_items"] = []
    assert "AI summary" in c.read_file("item:279")
    assert [r["itemId"] for r in mock.STATE["ai_requests"]] == ["279", "301", "279"]


def test_ai_text_only_renders_elements_visible_in_item(kms):
    mock = kms["mock"]
    text = client(kms).read_file("item:279")
    assert "E_RESTRICTED" not in mock.ITEMS["279"]["elements"]                 # the ai text alone has it
    assert any(e["element_name"] == "E_RESTRICTED" for e in mock.AI_ITEMS["279"]["elements"])
    assert mock.RESTRICTED_MARKER not in text and "Managers only" not in text
    assert "AI summary: verify the caller, then reset." in text and "AI links." in text


@pytest.mark.parametrize("case", [400, 422, 403, 500, "empty", "not_json", "error", "version_mismatch"])
def test_ai_text_item_failure_falls_back_to_item_html(kms, monkeypatch, caplog, case):
    mock = kms["mock"]
    caplog.set_level(logging.DEBUG, logger=kc.logger.name)
    # /public/item already proved this identity may read the item, so an ai answer about this one item
    # (a 4xx, a plain 500, an empty, non-JSON or ERROR 200) shows the item HTML instead, for this read only.
    if case == "version_mismatch":
        monkeypatch.setitem(mock.AI_ITEMS["279"], "version", 9)       # the mock's own 400
    else:
        mock.STATE["ai_status"] = case
    c = client(kms)
    text = c.read_file("item:279")
    assert "Item summary: verify the caller first." in text and "AI summary" not in text
    assert [r["itemId"] for r in mock.STATE["ai_requests"]] == ["279"]
    fell_back = [r.getMessage() for r in caplog.records if "fell back" in r.getMessage()]
    assert len(fell_back) == 1 and "279" not in fell_back[0]          # the kind only
    mock.STATE["ai_status"] = None
    second = c.read_file("item:301")
    assert "AI text for a billing dispute." in second and "Item text" not in second
    assert [r["itemId"] for r in mock.STATE["ai_requests"]] == ["279", "301"]


@pytest.mark.parametrize("make,case,kind,logins_added", [
    (client, 502, "server", 0), (client, 503, "server", 0), (client, 504, "server", 0),
    (client, 429, "throttled", 0), (token_client, 401, "auth", 0), (client, 401, "auth", 1),
    (token_client, 302, "redirect", 0), (client, "login_503", "server", 1), (client, "login_500", "server", 1),
], ids=["502", "503", "504", "429", "token_401", "session_refused_401", "token_302", "login_503", "login_500"])
def test_ai_text_global_failure_is_not_hidden(kms, make, case, kind, logins_added):
    mock = kms["mock"]
    make(kms).read_file("item:301")               # logs in (userpass); the session is pooled
    logins = mock.STATE["login_count"]
    login_status = {"login_503": 503, "login_500": 500}.get(case)
    if login_status:
        # The session expires at the ai call and the re-login fails: a login outage, not this item.
        mock.STATE["expire_sessions_at"] = "ai_answers_item"
        mock.STATE["login_status"] = login_status
    else:
        mock.STATE["ai_status"] = case
    with pytest.raises(kc.KmsHTTPError) as info:
        make(kms).read_file("item:279")
    assert info.value.kind == kind
    assert info.value.status == (login_status or case)
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert mock.STATE["login_count"] == logins + logins_added


def _with_ai_summary(mock, monkeypatch, text, item_html=None):
    """Item 279's E_SUMMARY as ai ``text`` and, in /public/item, as ``item_html``.

    The default ``item_html`` is what the editor saves for ``text`` typed as
    words: the same text with '&', '<' and '>' escaped. Pass ``item_html`` when
    the text is the editor's markup itself.
    """
    elements = [dict(e, element_text=text) if e["element_name"] == "E_SUMMARY" else e
                for e in mock.AI_ITEMS["279"]["elements"]]
    monkeypatch.setitem(mock.AI_ITEMS["279"], "elements", elements)
    value = html.escape(text, quote=False) if item_html is None else item_html
    _with_item_summary(mock, monkeypatch, value)


@pytest.mark.parametrize("authored", ["Enter <account number> in the field", "if a<b and c>d",
                                      "Use format <YYYY-MM-DD>"])
def test_ai_text_is_plain_text_shown_as_written(kms, monkeypatch, authored):
    mock = kms["mock"]
    # A tag-shaped '<' makes the element ambiguous, so it renders from its item HTML, where the
    # editor saved the placeholder escaped: it is shown as written.
    _with_ai_summary(mock, monkeypatch, authored)
    sections = _sections(client(kms).read_file("item:279"))
    assert f"### Summary\n{authored}\n" in sections["Overview"]


def test_a_plain_ai_element_is_shown_from_its_ai_text(kms, monkeypatch):
    mock = kms["mock"]
    # No tag-shaped '<' and no character reference: the ai text is used, not the item HTML.
    _with_ai_summary(mock, monkeypatch, "AI words: if a < b and c > d, press 5", item_html="<p>Editor words</p>")
    summary = _sections(client(kms).read_file("item:279"))["Overview"]
    assert "### Summary\nAI words: if a < b and c > d, press 5\n" in summary and "Editor words" not in summary


@pytest.mark.parametrize("ai,item_html,shown", [
    ("AI draft with <b>bold</b> words", "<p>Editor words with <b>bold</b></p>", "Editor words with bold"),
    ("AI draft Q&amp;A", "<p>Editor Q&amp;A</p>", "Editor Q&A"),
    ("AI draft</p>", "<p>Editor paragraph</p>", "Editor paragraph"),
    ("AI draft<!-- note -->", "<p>Editor note<!-- note --></p>", "Editor note"),
], ids=["tag", "entity", "closing_tag", "comment"])
def test_an_ambiguous_ai_element_renders_from_its_item_html(kms, monkeypatch, ai, item_html, shown):
    mock = kms["mock"]
    # One ambiguous element (a tag-shaped '<' or a character reference) shows its item-HTML rendering;
    # the other element of the same item is plain and still shows its ai text.
    _with_ai_summary(mock, monkeypatch, ai, item_html=item_html)
    sections = _sections(client(kms).read_file("item:279"))
    assert f"### Summary\n{shown}\n" in sections["Overview"] and "AI draft" not in sections["Overview"]
    assert "### Links\nAI links." in sections["Processes"]


@pytest.mark.parametrize("element", [{"tabId": "1", "title": "Summary", "type": "EDITOR_FIELD"},
                                     {"value": None, "tabId": "1", "title": "Summary", "type": "EDITOR_FIELD"}],
                         ids=["no_value", "null_value"])
def test_an_ambiguous_ai_element_with_no_item_html_is_dropped(kms, monkeypatch, caplog, element):
    mock = kms["mock"]
    caplog.set_level(logging.DEBUG, logger=kc.logger.name)
    _with_ai_summary(mock, monkeypatch, "AI draft <b>dropped-marker-3e71</b>")
    monkeypatch.setitem(mock.ITEMS["279"]["elements"], "E_SUMMARY", element)
    text = client(kms).read_file("item:279")
    assert "dropped-marker-3e71" not in text and "AI draft" not in text and "### Summary" not in text
    assert "### Links\nAI links." in _sections(text)["Processes"]       # the other element stays
    lines = [r.getMessage() for r in caplog.records if r.name == kc.logger.name and "dropped" in r.getMessage()]
    assert lines == ["kms_lighthouse ai_text dropped: an element has no item HTML"]


def test_ai_text_lines_and_blanks_are_normalised(kms, monkeypatch):
    mock = kms["mock"]
    _with_ai_summary(mock, monkeypatch, "  Step one:\t  open   the desk  \n\n\n\n  Step two \r\n")
    sections = _sections(client(kms).read_file("item:279"))
    assert "### Summary\nStep one: open the desk\n\nStep two\n" in sections["Overview"]


# A no-break space (U+00A0, "&nbsp;" in HTML) between a structured key and its value. Made-up values.
_NBSP_SECRETS = [
    ("client_secret:{nb}Xk29abcDEF", "Xk29abcDEF"),
    ('{{"client_secret":{nb}"Xk29abcDEF"}}', "Xk29abcDEF"),
    ("X-Api-Key:{nb}Zq81mnbVCX77", "Zq81mnbVCX77"),
    ("DB_PASSWORD={nb}Pw9xYz12QQ", "Pw9xYz12QQ"),
    ("api_key:{nb} sk_live_51HxABCDEF", "sk_live_51HxABCDEF"),
]


def _with_item_summary(mock, monkeypatch, html):
    monkeypatch.setitem(mock.ITEMS["279"]["elements"], "E_SUMMARY",
                        dict(mock.ITEMS["279"]["elements"]["E_SUMMARY"], value=html))


@pytest.mark.parametrize("template,secret", _NBSP_SECRETS, ids=[
    "client_secret", "json_client_secret", "x_api_key", "db_password", "api_key_sk_live"])
@pytest.mark.parametrize("ai_text", [True, False], ids=["ai_text", "item_html"])
def test_a_no_break_space_never_shields_a_secret(kms, monkeypatch, ai_text, template, secret):
    mock = kms["mock"]
    if ai_text:
        _with_ai_summary(mock, monkeypatch, template.format(nb=" "))
    else:
        mock.STATE["ai_text_available"] = False
        _with_item_summary(mock, monkeypatch, "<p>" + template.format(nb="&nbsp;") + "</p>")
    text = client(kms).read_file("item:279")
    assert ("AI links." in text) is ai_text        # the path under test really ran
    summary = _sections(text)["Overview"]
    assert secret not in text and "[redacted]" in summary
    assert " " not in text and "&nbsp;" not in text


@pytest.mark.parametrize("authored,secret,shown", [
    ("{&quot;api_key&quot;: &quot;sk_live_51HxABCDEF&quot;}", "sk_live_51HxABCDEF", '{"api_key": "[redacted]"}'),
    ("Authorization: Bearer <b>abc123def456</b>", "abc123def456", "Authorization: Bearer [redacted]"),
    ("Authorization: Bearer&nbsp;abc123def456", "abc123def456", "Authorization: Bearer [redacted]"),
    ("https://admin&#58;S3cretPw@host/x", "S3cretPw", "https://admin:[redacted]@host/x"),
    ("Use <b>api_key</b>: sk_live_51HxABCDEF", "sk_live_51HxABCDEF", "[redacted]"),
], ids=["entity_json", "bold_bearer", "nbsp_bearer", "entity_userinfo", "bold_key"])
def test_ai_text_that_carries_html_is_read_as_html(kms, monkeypatch, authored, secret, shown):
    mock = kms["mock"]
    # The API PDF says element_text is plain, but a tenant may hand back the editor's HTML: a tag-shaped
    # '<' or a character reference makes the element ambiguous, and it renders (scrubbed) from its item
    # HTML, which here is that same markup.
    _with_ai_summary(mock, monkeypatch, authored, item_html=authored)
    text = client(kms).read_file("item:279")
    assert "AI links." in text and "Item summary" not in text       # the ai answer, not the fallback
    summary = _sections(text)["Overview"]
    assert secret not in text and shown in summary
    assert "<b>" not in summary and "&quot;" not in summary and "&nbsp;" not in summary


@pytest.mark.parametrize("authored", [
    "Ask the caller for the <code> sent by SMS",
    "Ask for the <code> and the <account number>",
    "Set <var> to the branch id, format <YYYY-MM-DD>",
    "If <a> is larger than <b>, use <a>",
    "Press <p> to print",
    "Escalate to <HR> before closing",
    "Ask <hr> to sign, then <HR> files it",
], ids=["code", "code_and_account", "var_and_date", "a_and_b", "p", "hr_upper", "hr_both"])
def test_ai_text_with_a_tag_shaped_placeholder_is_shown_as_written(kms, monkeypatch, authored):
    mock = kms["mock"]
    # A tag-shaped placeholder makes the element ambiguous: it renders from its item HTML, where the
    # editor saved it escaped, so every <placeholder> in the element stays, whatever its name.
    _with_ai_summary(mock, monkeypatch, authored)
    text = client(kms).read_file("item:279")
    assert "AI links." in text and "Item summary" not in text       # the ai answer, not the fallback
    assert f"### Summary\n{authored}\n" in _sections(text)["Overview"]


@pytest.mark.parametrize("authored,item_html,secret,shown", [
    ("Ask for the <code>: Bearer <b>abc123def456</b>", "Ask for the &lt;code&gt;: Bearer <b>abc123def456</b>",
     "abc123def456", "Ask for the <code>: Bearer [redacted]"),
    ("Authorization: Bearer</td><td>abc123def456", None, "abc123def456", "Authorization: Bearer [redacted]"),
    ("api_key:<!-- -->sk_live_51HxABCDEF", None, "sk_live_51HxABCDEF", "api_key: [redacted]"),
    # The item HTML renderer turns a closing tag before ':' into a space, as on the item path.
    ('Use <span class="k">api_key</span>: sk_live_51HxABCDEF', None, "sk_live_51HxABCDEF",
     "Use api_key : [redacted]"),
    ('Use <span class="k">api_key: sk_live_51HxABCDEF', None, "sk_live_51HxABCDEF", "Use api_key: [redacted]"),
    ("Note<o:p></o:p> api_key: sk_live_51HxABCDEF", None, "sk_live_51HxABCDEF", "Note api_key: [redacted]"),
    ("Note</ b > Authorization: Bearer abc123def456", None, "abc123def456", "Note Authorization: Bearer [redacted]"),
    ("Rule<hr /> api_key: sk_live_51HxABCDEF", None, "sk_live_51HxABCDEF", "Rule api_key: [redacted]"),
], ids=["placeholder_and_bold", "table_cells", "comment", "attribute", "attribute_only", "office_paragraph",
        "closing_bold_only", "rule"])
def test_ai_text_with_markup_evidence_is_read_as_html_and_scrubbed(kms, monkeypatch, authored, item_html,
                                                                     secret, shown):
    mock = kms["mock"]
    # The item HTML is the markup itself (None) or, next to a placeholder, the editor's escaped form.
    _with_ai_summary(mock, monkeypatch, authored, item_html=authored if item_html is None else item_html)
    text = client(kms).read_file("item:279")
    assert "AI links." in text and "Item summary" not in text
    summary = _sections(text)["Overview"]
    assert secret not in text and f"### Summary\n{shown}\n" in summary


@pytest.mark.parametrize("authored,secret", [
    ("Authorization: Bearer<dt></dt><dd>abc123def456</dd>", "abc123def456"),
    ('api_key:<bdo dir="ltr">sk_live_51HxABCDEF</bdo>', "sk_live_51HxABCDEF"),
    ("Authorization: Bearer <nobr>abc123def456</nobr>", "abc123def456"),
    ("Authorization: Bearer<figcaption>abc123def456</figcaption>", "abc123def456"),
    ("Authorization: Bearer abc123<wbr>def456", "def456"),
    ("{&quot;api_key&quot;: &quot;sk_live_51HxABCDEF&quot;}", "sk_live_51HxABCDEF"),
    ("api_key:<!-- -->sk_live_51HxABCDEF", "sk_live_51HxABCDEF"),
    ("Authorization: Bearer</td><td>abc123def456", "abc123def456"),
], ids=["dt_dd", "bdo_dir", "nobr", "figcaption", "wbr", "quot_json", "comment", "table_cells"])
def test_an_ambiguous_ai_element_never_shows_a_secret(kms, monkeypatch, authored, secret):
    mock = kms["mock"]
    # Whatever the tag name: an element that holds markup renders from its item HTML, so no tag the
    # renderer does not list can sit between a key and its value.
    _with_ai_summary(mock, monkeypatch, authored, item_html=authored)
    text = client(kms).read_file("item:279")
    assert "AI links." in text and "Item summary" not in text       # the ai answer, not the fallback
    summary = _sections(text)["Overview"]
    assert secret not in text and "[redacted]" in summary and "<" not in summary


@pytest.mark.parametrize("authored", ["Step one<br>Step two", "Step one<br/>Step two", "Step one<BR />Step two"])
def test_ai_text_with_a_line_break_tag_is_read_as_html(kms, monkeypatch, authored):
    mock = kms["mock"]
    _with_ai_summary(mock, monkeypatch, authored, item_html=authored)
    text = client(kms).read_file("item:279")
    assert "AI links." in text and "### Summary\nStep one\nStep two\n" in _sections(text)["Overview"]


@pytest.mark.parametrize("authored", [
    "Set <timeout>30</timeout> in web.config",
    "Send <Request><CustomerId>12345</CustomerId></Request> to the service",
    "In settings.xml set <password>${MAVEN_PASS}</password>",
    'Add <add key="Mode" value="1"/> under </appSettings>',
    "The bucket answers <Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>",
], ids=["timeout", "request", "maven_password", "app_settings", "cloud_error"])
def test_ai_text_with_an_xml_example_is_shown_as_written(kms, monkeypatch, authored):
    mock = kms["mock"]
    # An XML or config example makes the element ambiguous: it renders from its item HTML, where the
    # editor saved the example escaped, so it keeps its tags, whatever their names.
    _with_ai_summary(mock, monkeypatch, authored)
    text = client(kms).read_file("item:279")
    assert "AI links." in text and "Item summary" not in text       # the ai answer, not the fallback
    assert f"### Summary\n{authored}\n" in _sections(text)["Overview"]


@pytest.mark.parametrize("authored,secrets,shown", [
    ("Authorization: Bearer abc123<wbr>def456", ("abc123", "def456"), "Authorization: Bearer [redacted]\n"),
    # A secret query parameter is dropped whole, as on every path, so the URL shows no "[redacted]".
    ("https://kms.example.com/contents?token=Sv1x9Qk2ab<wbr>cdEF77gh", ("Sv1x9Qk2ab", "cdEF77gh"),
     "### Summary\nhttps://kms.example.com/contents\n"),
    ("eyJhbGci<wbr>OiJub25lIn0.eyJzdWIiOiJjb3JwdXMifQ.Y29ycHVzLXNpZ25lZA",
     ("OiJub25lIn0", "eyJzdWIiOiJjb3JwdXMifQ", "Y29ycHVzLXNpZ25lZA"), "### Summary\n[redacted]\n"),
], ids=["bearer", "query_token", "jwt"])
def test_ai_text_with_a_word_break_tag_is_read_as_html_and_scrubbed(kms, monkeypatch, authored, secrets, shown):
    mock = kms["mock"]
    # A bare void tag ("<wbr>") is markup: the element renders from its item HTML (that same markup),
    # where the token it splits is joined, then scrubbed.
    _with_ai_summary(mock, monkeypatch, authored, item_html=authored)
    text = client(kms).read_file("item:279")
    assert "AI links." in text and "Item summary" not in text
    summary = _sections(text)["Overview"]
    assert shown in summary and "<wbr>" not in summary
    for secret in secrets:
        assert secret not in text, secret


def test_ai_text_fall_back_logs_a_dropped_element_once(kms, monkeypatch, caplog):
    mock = kms["mock"]
    caplog.set_level(logging.DEBUG, logger=kc.logger.name)
    monkeypatch.setitem(mock.ITEMS["279"]["elements"], "E_ODD", "odd-element-marker-5c13")
    monkeypatch.setitem(mock.AI_ITEMS["279"], "elements", [
        {"element_name": "E_RESTRICTED", "element_caption": "Managers only", "tab_caption": "Overview",
         "element_text": "restricted"}])
    text = client(kms).read_file("item:279")
    assert "Item summary: verify the caller first." in text and "restricted" not in text
    lines = [r.getMessage() for r in caplog.records if r.name == kc.logger.name and "dropped" in r.getMessage()]
    assert lines == ["kms_lighthouse item dropped: an element is not a record"]


@pytest.mark.parametrize("ai_text", [True, False], ids=["ai_text", "item_html"])
def test_an_item_with_an_empty_element_list_reads_as_no_elements(kms, monkeypatch, ai_text):
    mock = kms["mock"]
    mock.STATE["ai_text_available"] = ai_text
    monkeypatch.setitem(mock.ITEMS["279"], "elements", [])        # an empty list is no elements, like {}
    text = client(kms).read_file("item:279")
    assert text.splitlines()[0] == "Title: Reset a customer password [redacted]"
    assert not [line for line in text.splitlines() if line.startswith("## ")]
    assert "AI summary" not in text and "Item summary" not in text


@pytest.mark.parametrize("elements", [[], ["odd"], "odd", 7])
def test_an_offline_item_is_not_found_whatever_its_elements(kms, monkeypatch, elements):
    mock = kms["mock"]
    monkeypatch.setitem(mock.ITEMS["306"], "elements", elements)
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms).read_file("item:306")
    assert (info.value.status, info.value.kind) == (404, "not_found")
    assert info.value.__context__ is None and mock.STATE["ai_requests"] == []


def test_an_element_that_is_not_a_record_is_dropped_and_logged(kms, monkeypatch, caplog):
    mock = kms["mock"]
    caplog.set_level(logging.DEBUG, logger=kc.logger.name)
    mock.STATE["ai_text_available"] = False
    marker = "odd-element-marker-5c13"
    monkeypatch.setitem(mock.ITEMS["279"]["elements"], "E_ODD", marker)
    text = client(kms).read_file("item:279")
    assert "Item summary: verify the caller first." in text and marker not in text
    lines = [r.getMessage() for r in caplog.records if r.name == kc.logger.name and "dropped" in r.getMessage()]
    assert lines and set(lines) == {"kms_lighthouse item dropped: an element is not a record"}
    assert all(marker not in r.getMessage() and "E_ODD" not in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("change,reason", [
    ({"item_id": 280}, "not the item asked for"),
    ({"elements": [{"element_name": "E_RESTRICTED", "element_caption": "Managers only",
                    "tab_caption": "Overview", "element_text": "restricted"}]}, "no element the item shows"),
])
def test_dropped_ai_text_is_logged_without_ids_or_content(kms, monkeypatch, caplog, change, reason):
    mock = kms["mock"]
    caplog.set_level(logging.DEBUG, logger=kc.logger.name)
    for key, value in change.items():
        monkeypatch.setitem(mock.AI_ITEMS["279"], key, value)
    text = client(kms).read_file("item:279")
    assert "Item summary: verify the caller first." in text and "restricted" not in text
    lines = [r.getMessage() for r in caplog.records if r.name == kc.logger.name and "fell back" in r.getMessage()]
    assert lines == [f"kms_lighthouse ai_text fell back: {reason}"]


def test_item_elements_that_are_not_a_record_map_fail_closed(kms, caplog):
    mock = kms["mock"]
    caplog.set_level(logging.DEBUG, logger=kc.logger.name)
    mock.STATE["ai_text_available"] = False
    mock.STATE["odd_shapes"] = "bad_elements"
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms).read_file("item:279")
    assert (info.value.status, info.value.kind) == (200, "server")
    assert mock.ODD_MARKER not in str(info.value) and info.value.__context__ is None
    assert any(r.getMessage() == "kms_lighthouse item refused: elements is not a record map"
               for r in caplog.records)


@pytest.mark.parametrize("file_id", ["item:279", "279"])
def test_login_failure_on_read_is_not_an_item_kind(kms, file_id):
    mock = kms["mock"]
    # A login that answers 404 says nothing about item 279; it is no reason to try a scenario either.
    mock.STATE["login_status"] = 404
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms).read_file(file_id)
    assert (info.value.status, info.value.kind) == (404, "server")
    assert str(info.value) == kc._MESSAGES["server"]
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert _scenario_rows(kms) == [] and mock.STATE["login_count"] == 1


def test_file_version_is_not_implemented(kms):
    c = client(kms)
    assert c.file_version("item:279") is None
    assert type(c).file_version is DataSourceClient.file_version
    assert kms["mock"].REQUEST_LOG == []


def test_execute_query_delegates_to_read_file(kms):
    mock = kms["mock"]
    c = client(kms)
    assert c.execute_query('{"file_id": "item:279"}') == c.read_file("item:279")
    assert c.execute_query("item:301") == c.read_file("item:301")
    mock.REQUEST_LOG.clear()
    for query in ('{"file_id": ', '{"file_id": "item:279"', '["item:279"]', '{"path": "item:279"}', "item:x"):
        with pytest.raises(kc.KmsError) as info:
            client(kms).execute_query(query)
        assert "279" not in str(info.value), query
        assert info.value.__context__ is None and info.value.__cause__ is None
    assert mock.REQUEST_LOG == []
    # The catalog members make no request either.
    assert c.get_schemas() == [] and c.get_schema("anything") is None and c.prompt_schema() == ""
    events = []
    assert c.get_schemas(progress_callback=lambda *e: events.append(e)) == []
    assert events and events[-1][0] == "catalog_ready"
    assert mock.REQUEST_LOG == []


@pytest.mark.parametrize("case", ["bare_url", "entity_url", "body_jwt", "title", "description"])
@pytest.mark.parametrize("ai_text", [True, False], ids=["ai_text", "item_html"])
def test_article_text_scrubs_token_urls(kms, ai_text, case):
    mock = kms["mock"]
    mock.STATE["ai_text_available"] = ai_text
    text = client(kms).read_file("item:279")
    assert ("AI links." in text) is ai_text        # the path under test really ran
    assert mock.ARTICLE_SECRETS[case] not in text
    assert "eyJ" not in text and "token=" not in text and mock.ARTICLE_SECRETS["img"] not in text
    # Only the secret parameter goes; the rest of each URL and the words around it stay. The item HTML
    # carries "&amp;" (decoded), the plain ai text a raw '&'.
    assert "Form at https://kms.example.com/contents or https://kms.example.com/contents?id=7&lang=en." in text
    assert "Signed [redacted]" in text


def test_read_offline_item_is_not_found(kms):
    mock = kms["mock"]
    for file_id in ("item:306", "306"):
        with pytest.raises(kc.KmsHTTPError) as info:
            client(kms).read_file(file_id)
        assert info.value.kind == "not_found", file_id
        assert info.value.__context__ is None and info.value.__cause__ is None
    assert _scenario_rows(kms) == []               # a hidden item is no reason to switch kind
    assert mock.STATE["ai_requests"] == []         # hidden before any ai text is asked for
    text = client(kms, include_offline=True).read_file("item:306")
    assert text.splitlines()[0] == "Title: Draft refund rules" and "AI draft text." in text


def test_bare_id_falls_back_to_scenario_once(kms):
    mock = kms["mock"]
    text = client(kms).read_file("21302")
    assert "Kind: scenario (scenario:21302)" in text.splitlines()
    assert text.splitlines()[0] == "Title: How to Register for Advanced Credit Card Security"
    assert text.name == "How to Register for Advanced Credit Card Security.txt"
    assert len(_rows(kms, "/public/item/21302")) == 1 and len(_scenario_rows(kms)) == 1
    # An explicit item id never switches kind.
    mock.REQUEST_LOG.clear()
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms).read_file("item:21302")
    assert info.value.kind == "not_found" and _scenario_rows(kms) == []
    # Neither an item nor a scenario: exactly one scenario request, then not found.
    mock.REQUEST_LOG.clear()
    with pytest.raises(kc.KmsHTTPError) as info:
        client(kms).read_file("99999")
    assert info.value.kind == "not_found"
    assert len(_rows(kms, "/public/item/99999")) == 1 and len(_rows(kms, "/public/scenario/99999")) == 1
    # A bare id that is an item reads the item.
    mock.REQUEST_LOG.clear()
    assert "Kind: article (T_PROCEDURE)" in client(kms).read_file("279").splitlines()
    assert _scenario_rows(kms) == []


@pytest.mark.parametrize("method", ["read_file", "read_raw_bytes"])
def test_read_folder_id_is_refused(kms, method):
    with pytest.raises(kc.KmsError) as info:
        getattr(client(kms), method)("folder:10")
    assert str(info.value) == _FOLDER_READ
    assert kms["mock"].REQUEST_LOG == []


@pytest.mark.parametrize("file_id", ["item:abc", "../279", "item:279/../1", "279 ", "item:", "x" * 50,
                                     "scenario:1e3", "item:1234567890123"])
def test_read_bad_id_is_refused_before_any_request(kms, file_id):
    with pytest.raises(kc.KmsError) as info:
        client(kms).read_file(file_id)
    assert str(info.value) == kc._BAD_ID
    assert kms["mock"].REQUEST_LOG == []


def test_read_result_renders_as_text(kms):
    payload = client(kms).read_file("item:279")
    rendered = render_file_payload(name=payload_name(payload, "item:279"), payload=payload, max_rows=50,
                                   max_chars=20000)
    assert rendered["content_type"] == "text"
    assert rendered["file_name"] == "Reset a customer password [redacted].txt"
    assert rendered["text"] == str(payload) and rendered["truncated"] is False


def test_read_raw_bytes_respects_max_bytes(kms):
    c = client(kms)
    text = c.read_file("item:279")
    data, name, mime = c.read_raw_bytes("item:279")
    assert data == text.encode("utf-8") and name == text.name and mime == "text/plain"
    assert c.read_raw_bytes("item:279", max_bytes=len(data))[0] == data
    with pytest.raises(FileTooLargeError) as info:
        c.read_raw_bytes("item:279", max_bytes=len(data) - 1)
    assert "279" not in str(info.value)
    assert info.value.__context__ is None and info.value.__cause__ is None


@pytest.mark.parametrize("value,shown", [
    ("10/04/2024", "2024-04-10"),                    # day first
    ("2024-05-09 12:57:56.0", "2024-05-09T12:57:56"),
    ("2024-05-09T12:57:56", "2024-05-09T12:57:56"),
    ("31/02/2024", None), ("not a date", None),
])
def test_dates_are_normalised(kms, monkeypatch, value, shown):
    monkeypatch.setitem(kms["mock"].ITEMS["279"], "updateDate", value)
    lines = client(kms).read_file("item:279").splitlines()
    assert [line for line in lines if line.startswith("Updated:")] == ([f"Updated: {shown}"] if shown else [])


# ------------------------------------------------------------------ closed errors
_METHODS = {
    "search": lambda c: c.search_files("billing"),
    "list": lambda c: c.list_files(),
    "read": lambda c: c.read_file("item:279"),
    "read_raw_bytes": lambda c: c.read_raw_bytes("item:279"),
}


@pytest.mark.parametrize("shape", ["list", "error", "bad_id"])
@pytest.mark.parametrize("method", sorted(_METHODS))
@pytest.mark.parametrize("make", [client, token_client])
def test_odd_shapes_never_echo_provider_text(kms, make, method, shape):
    mock = kms["mock"]
    mock.STATE["odd_shapes"] = shape
    # A search answer whose every record is bad ("bad_id") is an unexpected answer, not "no hits".
    with pytest.raises(kc.KmsError) as info:
        _METHODS[method](make(kms))
    err = info.value
    assert mock.ODD_MARKER not in str(err) and mock.ODD_MARKER not in repr(err)
    assert not [m for m in _AUTH_ERROR_MARKERS if m in str(err).lower()]
    assert err.__context__ is None and err.__cause__ is None


# Every public method that can touch the network.
_PUBLIC_METHODS = ("test_connection", "search_files", "list_files", "read_file", "read_raw_bytes", "execute_query")


@pytest.mark.parametrize("name", _PUBLIC_METHODS)
def test_public_methods_close_their_errors(name):
    assert hasattr(getattr(kc.KmsLighthouseClient, name), "__wrapped__"), name


# ------------------------------------------------------------------ scrub: bypass corpus
_URL = "https://kms.example.com/contents"
# Every name the scrub treats as secret, in the spelling a page might use.
_SECRET_NAMES = ("token", "access_token", "id_token", "refresh_token", "jwt", "auth", "password", "passwd",
                 "pwd", "api_key", "apikey", "key", "secret", "sig", "signature", "code", "jsessionid",
                 "session", "sessionid", "X-Amz-Security-Token", "X-Amz-Signature", "X-Amz-Credential",
                 "client_secret", "auth_token", "private_token", "access_key", "ticket")
_COMPOUND_SECRET_NAMES = ("newPassword", "apiToken", "refreshToken", "secret_key", "auth_key", "private_key",
                          "aws_secret_access_key", "user-passwd", "x-api-key", "AccessKey", "php_sessionid",
                          "subscription-key", "Ocp-Apim-Subscription-Key")


def _nested(text, rounds):
    """`text` percent-encoded `rounds` times (the {s} placeholder stays literal)."""
    for _ in range(rounds):
        text = quote(text, safe="{}")
    return text


# (form, text with {s} where the secret value goes, or {s1} and {s2} for its two halves)
_SECRET_FORMS = [
    ("fragment", _URL + "#access_token={s}&token_type=bearer"),
    ("semicolon", _URL + "?id=7;token={s};lang=en"),
    ("camel_case", _URL + "?accessToken={s}"),
    ("kebab_case", _URL + "?access-token={s}"),
    ("upper_case", _URL + "?TOKEN={s}"),
    ("repeated_amp", _URL + "?id=7&amp;amp;amp;token={s}"),
    ("nested_url", _URL + "?next=https%3A%2F%2Fkms.example.com%2Fx%3Ftoken%3D{s}"),
    ("double_encoded_nested_url", _URL + "?next=https%253A%252F%252Fkms.example.com%252Fx%253Ftoken%253D{s}"),
    ("array_name", _URL + "?token[]={s}"),
    ("encoded_array_name", _URL + "?token%5B%5D={s}"),
    ("encoded_equals", _URL + "?token%3D{s}"),
    ("encoded_ampersand", _URL + "?id=7%26token%3D{s}"),
    ("encoded_name", _URL + "?acc%65ss_token={s}"),
    ("path_parameter", _URL + ";jsessionid={s}"),
    ("bearer", "Authorization: Bearer {s}"),
    # A tag, a line break or a space between the name, the '=' and the value.
    ("br_after_equals", _URL + "?token=<br>{s}"),
    ("wbr_after_equals", _URL + "?token=<wbr>{s}"),
    ("newline_after_equals", _URL + "?token=\n{s}"),
    ("space_after_equals", _URL + "?token= {s}"),
    ("tab_after_equals_other_name", _URL + "?id=7&jsessionid=\t{s}"),
    ("span_around_value", _URL + "?token=<span>{s}</span>"),
    ("wbr_inside_value", _URL + "?token={s1}<wbr>{s2}"),
    ("bold_inside_value", _URL + "?token={s1}<b></b>{s2}"),
    ("wbr_inside_name", _URL + "?id=1&acc<wbr>ess_token={s}"),
    # Quoted, entity-escaped and JSON-escaped forms.
    ("single_quoted_value", _URL + "?token='{s}'"),
    ("double_quoted_value", _URL + '?token="{s}"'),
    ("entity_equals", _URL + "?token&amp;#61;{s}"),
    ("json_escaped_ampersand", _URL + "?id=1\\u0026token={s}"),
    # Nested deeper than the decoding rounds: the part is dropped, not kept encoded.
    ("nested_beyond_depth", _URL + "?next=" + _nested("https://kms.example.com/x?token={s}", 7)),
    # Outside query syntax.
    ("url_userinfo", "https://user:{s}@kms.example.com/x"),
    ("authorization_basic", "Authorization: Basic {s}"),
    ("bare_basic", "send Basic {s} as the header"),
    # Structured key/value (config, env, YAML): an identifier-shaped secret name. Plain English
    # labels ("Password: ...") are authored prose and are shown as written (user decision A).
    ("structured_db_password_equals", "db_password={s}"),
    ("structured_api_key_equals", "api_key={s}"),
    ("structured_client_secret_colon", "clientSecret: {s}"),
    ("structured_api_token_colon", "apiToken: {s}"),
    ("structured_secret_key_spaced", "secret_key = {s}"),
    ("structured_aws_secret_access_key", "aws_secret_access_key = {s}"),
    ("structured_private_key", "private_key: {s}"),
    ("structured_header_api_key", "X-Api-Key: {s}"),
    ("structured_cli_flag", "run it with --auth-token={s} once"),
    ("structured_dotted_config", "spring.datasource.db_password={s}"),
    ("json_new_password", '{{"newPassword": "{s}"}}'),
    ("json_quoted_plain_name", '{{"token": "{s}", "lang": "en"}}'),
    ("json_unquoted_value", '{{"apiToken":{s},"lang":"en"}}'),
    ("python_single_quoted", "{{'api_key': '{s}'}}"),
    # An empty name before a secret one must not swallow it.
    ("empty_value_before_token", _URL + "?q= &token= {s}"),
    ("empty_value_before_password", _URL + "?lang= &password= {s}"),
    ("repeated_spaced_token", _URL + "?token= ?token= {s}"),
    ("quote_opened_before_token", _URL + '?a="x ?token= {s} "'),
    # More inline tags that a page may put inside a value.
    ("code_inside_value", _URL + "?token={s1}<code></code>{s2}"),
    ("kbd_inside_value", _URL + "?token={s1}<kbd></kbd>{s2}"),
    ("named_entity_equals", _URL + "?token&amp;equals;{s}"),
    # JSON or YAML quoted names, and a quoted value of several words.
    ("json_password", '{{"password": "{s}"}}'),
    ("json_api_key", '"api_key":"{s}"'),
    ("json_multi_word_value", '{{"password": "{s1} {s2}"}}'),
    ("yaml_single_quoted", "config:\n  'secret': '{s}'"),
    ("dict_single_quoted_plain_name", "{{'secret': '{s}'}}"),
    ("json_braced_api_key", '{{"api_key":"{s}"}}'),
    ("json_after_comma", '{{"lang": "en", "password": "{s}"}}'),
    # A scheme word taken as a structured value: the credential after it goes too.
    ("structured_bearer_value", "X-Auth-Token: Bearer {s}"),
    ("structured_basic_value", "x-api-key: Basic {s}"),
    ("structured_token_scheme_value", "private_token: Token {s}"),
    # Smart quotes around the key and the value.
    ("smart_double_quoted", "“api_key”: “{s}”"),
    ("smart_single_quoted", "{{‘password’: ‘{s}’}}"),
    # Acronym camel case, dotted config keys, CLI flags and x- headers.
    ("structured_acronym_api_key", "APIKey: {s}"),
    ("structured_acronym_api_token", "APIToken: {s}"),
    ("structured_acronym_api_secret", "APISecret: {s}"),
    ("structured_dotted_password", "spring.datasource.password={s}"),
    ("structured_short_dotted_password", "db.password={s}"),
    ("structured_cli_password", "run it with --password={s} once"),
    ("structured_lower_x_api_key", "x-api-key: {s}"),
    ("structured_x_auth_token", "X-Auth-Token: {s}"),
    ("structured_x_amz_header", "X-Amz-Security-Token: {s}"),
    ("structured_mysql_pwd", "MYSQL_PWD={s}"),
    ("structured_hyphen_compound", "client-secret: {s}"),
    # Numeric references without their ';', and a bare "&amp" before a name.
    ("entity_equals_unterminated", _URL + "?token&#61{s}"),
    ("hex_entity_equals_unterminated", _URL + "?token&#x3d{s}"),
    ("bare_amp_before_name", _URL + "?id=1&ampsig={s}"),
    ("entity_ampersand_unterminated", _URL + "?id=1&#38sig={s}"),
    ("structured_session_id", "session_id: {s}"),
    # A raw '@' inside a URL password, and a scheme right after a digit, '.' or '-'.
    ("userinfo_at_inside_password", "https://user:{s1}@{s2}@kms.example.com/x"),
    ("userinfo_after_digit", "7https://user:{s}@kms.example.com/x"),
    ("userinfo_after_dot", "1.https://user:{s}@kms.example.com/x"),
    ("userinfo_after_dash", "--https://user:{s}@kms.example.com/x"),
    # A dotted config key whose name starts in lower case (camel case after the dot).
    ("structured_dotted_camel", "app.apiKey: {s}"),
    # JSON escaped inside a JSON string: a backslash before each quote of the key and the value.
    ("escaped_json_body", '"body": "{{\\"username\\":\\"u\\",\\"password\\":\\"{s}\\"}}"'),
    ("escaped_json_api_key", '{{\\"api_key\\":\\"{s}\\"}}'),
    ("escaped_json_client_secret_spaced", '{{\\"client_secret\\": \\"{s}\\"}}'),
    # A quoted name whose closing quote does not pair with its opener.
    ("smart_open_ascii_close_name", '“api_key": "{s}"'),
    ("ascii_open_smart_close_name", '"api_key”: "{s}"'),
    ("smart_open_plain_name", '{{“password": "{s}"}}'),
    # Command examples: their "-u"/"--user" flags are authored text (decision A, round 9); a header,
    # a URL password, a secret parameter or a "--...password=" key in them still goes.
    ("curl_header_bearer", "curl -H 'Authorization: Bearer {s}' https://kms.example.com/x"),
    ("curl_header_bearer_double_quoted", 'curl -H "Authorization: Bearer {s}" https://kms.example.com/x'),
    ("curl_oauth2_bearer_quoted", 'curl --oauth2-bearer "{s}" https://kms.example.com/x'),
    ("curl_userinfo", "curl https://admin:{s}@kms.example.com/x"),
    ("curl_data_access_token", "curl -d 'access_token={s}' https://kms.example.com/x"),
    ("curl_header_api_key", "curl -H 'X-Api-Key: {s}' https://kms.example.com/x"),
    ("wget_password_equals", "wget --password={s} https://kms.example.com/x"),
    # A compound name ending with "pass" after a listed service word, and a subscription key header.
    ("structured_db_pass", "DB_PASS={s}"),
    ("structured_smtp_pass", "SMTP_PASS: {s}"),
    ("structured_smtp_pass_equals", "SMTP_PASS={s}"),
    ("structured_hyphen_db_pass", "db-pass: {s}"),
    ("structured_hyphen_smtp_pass", "smtp-pass: {s}"),
    ("structured_camel_db_pass", "dbPass: {s}"),
    ("structured_camel_smtp_pass", "smtpPass={s}"),
    ("structured_mysql_root_pass", "MYSQL_ROOT_PASS={s}"),
    ("structured_redis_pass", "REDIS_PASS={s}"),
    ("structured_subscription_key_header", "Ocp-Apim-Subscription-Key: {s}"),
    ("structured_subscription_key", "subscription-key: {s}"),
] + [(f"name_{name}", _URL + f"?id=7&{name}={{s}}&lang=en") for name in _SECRET_NAMES] + [
    # Compound and camel-case names: secret when the name ends with a secret word.
    (f"compound_{name}", _URL + f"?id=7&{name}={{s}}&lang=en") for name in _COMPOUND_SECRET_NAMES
] + [(f"compound_spaced_{name}", _URL + f"?q= &{name}= {{s}}") for name in _COMPOUND_SECRET_NAMES]

_JWT = ("eyJhbGciOiJub25lIn0", "eyJzdWIiOiJjb3JwdXMifQ", "Y29ycHVzLXNpZ25lZA")
_H, _P, _S = _JWT
# A JWT split by something invisible, or by a tag that strip_html turns into whitespace.
_JWT_FORMS = [
    ("soft_hyphen_before_dot", f"{_H}­.{_P}.{_S}"),
    ("soft_hyphen_inside", f"{_H[:8]}­{_H[8:]}.{_P}.{_S}"),
    ("shy_entity", f"{_H}&shy;.{_P}.{_S}"),
    ("zero_width_space", f"{_H}.{_P}​.{_S}"),
    ("word_joiner", f"{_H}⁠.{_P}.{_S}"),
    ("nul", f"{_H}.{_P}\x00.{_S}"),
    ("wbr_tag", f"{_H}<wbr>.{_P}.{_S}"),
    ("br_tag", f"{_H}<br>.{_P}.{_S}"),
    # Private-use, unassigned and surrogate characters next to a dot, and tags inside a segment.
    ("private_use_before_dot", f"{_H}\ue000.{_P}.{_S}"),
    ("private_use_entity", f"{_H}&#xE000;.{_P}.{_S}"),
    ("unassigned_after_dot", f"{_H}.\u0378{_P}.{_S}"),
    ("surrogate_before_dot", f"{_H}.{_P}\ud800.{_S}"),
    ("wbr_inside_segment", f"{_H[:8]}<wbr>{_H[8:]}.{_P}.{_S}"),
    ("empty_span_inside_segment", f"{_H}.{_P[:9]}<span></span>{_P[9:]}.{_S}"),
    ("empty_code_inside_segment", f"{_H}.{_P[:9]}<code></code>{_P[9:]}.{_S}"),
    # A line break or a block tag inside the header: the payload and the signature (which start a
    # JWT of their own) are still redacted; the rest of the header after the break is shown.
    ("br_inside_segment", f"{_H[:10]}<br>{_H[10:]}.{_P}.{_S}"),
    ("paragraph_inside_segment", f"{_H[:10]}<p>{_H[10:]}.{_P}.{_S}"),
] + [
    # Default-ignorable characters that are not in a C category (invisible, yet letters or marks).
    (f"ignorable_{cp:04X}", f"{_H[:8]}{chr(cp)}{_H[8:]}.{_P}.{_S}")
    for cp in (0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180B, 0x180F, 0x3164, 0xFE00, 0xFE0F, 0xFFA0,
               0xE0100, 0xE01EF)
]


def _search_one(kms, text):
    """Serve one record carrying `text` in its title and description; return its entry."""
    raw_id = str(uuid.uuid4().int)[:9]
    kms["mock"].STATE["extra_results"] = [_record(raw_id, "Guide " + text, description="Guide " + text + " end")]
    return _by_id(client(kms).search_files("x"))[f"item:{raw_id}"]


@pytest.mark.parametrize("form,template", _SECRET_FORMS, ids=[f for f, _ in _SECRET_FORMS])
def test_search_scrubs_secret_parameters_in_every_form(kms, form, template):
    secret = "Sv" + uuid.uuid4().hex[:8] + "9Q"
    halves = (secret[:6], secret[6:])
    entry = _search_one(kms, template.format(s=secret, s1=halves[0], s2=halves[1]))
    for field in ("name", "path", "snippet"):
        for piece in (secret, *halves):
            assert piece.lower() not in entry[field].lower(), (form, field, entry[field])
    assert "Guide" in entry["snippet"] and "Guide" in entry["name"]      # the readable text stays


@pytest.mark.parametrize("form,text", _JWT_FORMS, ids=[f for f, _ in _JWT_FORMS])
def test_search_redacts_jwts_split_by_invisible_characters(kms, form, text):
    entry = _search_one(kms, text)
    # The header is not secret: a line break inside it may leave its tail shown (no tail rule).
    pieces = (_P[3:], _S) if form in ("br_inside_segment", "paragraph_inside_segment") else (
        _H[3:], _H[8:], _H[10:], _P[3:], _S)
    for field in ("name", "path", "snippet"):
        value = entry[field]
        assert "eyJ" not in value, (form, field, value)
        for piece in pieces:
            assert piece not in value, (form, field, value)
    assert "Guide" in entry["snippet"]


# Ordinary words right after a redaction stay, including ones with digits, capitals and hyphens
# that a secret could look like.
@pytest.mark.parametrize("text,secret,kept", [
    (_URL + "?token=Sv1x9Qk2\nDocumentation overview", "Sv1x9Qk2", "Documentation overview"),
    (_URL + "?token= Sv1x9Qk2 then the rest", "Sv1x9Qk2", "then the rest"),
    (f"{_H}.{_P}.{_S} Overview of the steps", _S, "Overview of the steps"),
    ("db_password: Sv1x9Qk2abc\nKnowledge base", "Sv1x9Qk2abc", "Knowledge base"),
    (_URL + "?token=Sv1x9Qk2 COVID-19 guide", "Sv1x9Qk2", "COVID-19 guide"),
    (f"{_H}.{_P}.{_S} PowerShell steps", _S, "PowerShell steps"),
    ("Authorization: Bearer Sv1x9Qk2 Office365 login", "Sv1x9Qk2", "Office365 login"),
    ('"api_key": "Sv1x9Qk2" iPhone15 app', "Sv1x9Qk2", "iPhone15 app"),
    (_URL + "?id=7&token=Sv1x9Qk2\nFollow-up steps", "Sv1x9Qk2", "Follow-up steps"),
    ("apiToken: Sv1x9Qk2 Self-service portal", "Sv1x9Qk2", "Self-service portal"),
    ("Authorization: Basic U3YxeDlRazI6YWJj 2026-11-04 rollout", "U3YxeDlRazI6YWJj", "2026-11-04 rollout"),
    (_URL + "?token=Sv1x9Qk2 Follow-up", "Sv1x9Qk2", "Follow-up"),
    # The scheme word is read with its credential, so the credential goes and the next word stays.
    ("X-Auth-Token: Bearer Sv1x9Qk2abc next", "Sv1x9Qk2abc", "next"),
    ("x-api-key: Basic U3YxeDlRazI6YWJj next", "U3YxeDlRazI6YWJj", "next"),
    ("X-Auth-Token: Bearer AbcdefghijklMnop next", "AbcdefghijklMnop", "next"),
    # An unquoted structured value stops at '&' and ';': the parameters after it stay.
    ("client_secret=Sv1x9Qk2&lang=en done", "Sv1x9Qk2", "&lang=en done"),
    ("api_key: Sv1x9Qk2;lang=en", "Sv1x9Qk2", ";lang=en"),
    # A command example keeps the rest of the command when a header or a URL in it holds the secret.
    ("curl -H 'Authorization: Bearer Sv1x9Qk2' https://kms.example.com/x", "Sv1x9Qk2", "' https:"),
    ("curl https://reader:Sv1x9Qk2@kms.example.com/x -X GET", "Sv1x9Qk2", "-X GET"),
])
def test_search_keeps_ordinary_words_after_a_redaction(kms, text, secret, kept):
    entry = _search_one(kms, text)
    for field in ("name", "snippet"):
        assert kept in entry[field], (field, entry[field])
        assert secret not in entry[field], (field, entry[field])


@pytest.mark.parametrize("template", [_URL + "?token='{s} and the rest of the guide",
                                      'api_key: "{s} and the rest of the guide',
                                      '{{"password": "{s} and the rest of the guide'])
def test_search_unclosed_quote_does_not_hide_the_rest(kms, template):
    secret = "Sv" + uuid.uuid4().hex[:8] + "9Q"
    entry = _search_one(kms, template.format(s=secret))
    assert secret not in entry["snippet"], entry["snippet"]
    assert "and the rest of the guide" in entry["snippet"], entry["snippet"]


@pytest.mark.parametrize("text", [
    # Decision A (2026-11-05): a credential flag in a command example is authored text, shown as written.
    "curl -u admin:SECRETX1 x", "curl --user admin:SECRETX1 x", "curl --user=admin:SECRETX1 x",
    "curl -uadmin:SECRETX1 x", "curl -u 'admin:SECRETX1' x", 'curl -u "admin:SECRETX1" x',
    "curl -su admin:SECRETX1 x", "curl -sku admin:SECRETX1 x", "curl -vu admin:SECRETX1 x",
    "curl -u admin:'SECRETX1' x", 'curl -u admin:"SECRETX1" x', "curl -u admin:'SECRETX1 SECRETX2' x",
    'curl -u admin:"SECRETX1 SECRETX2" x', "curl -U proxy:SECRETX1 x", "curl --proxy-user proxy:SECRETX1 x",
    "curl --proxy-user=proxy:SECRETX1 x",
    "curl https://api.stripe.com/v1/charges -u sk_demo_4eC39HqLyjWDarjtT1zdp7dc:",
    "curl -u sk_live_X1:", "curl -u KEY12345: -d a=b", "wget --password SECRETX1 x",
    "mysql -u root -pSECRETX1 db", "curl -x user:SECRETX1@proxy:3128 https://x",
])
def test_search_shows_command_line_credentials_as_written(kms, text):
    entry = _search_one(kms, text)
    assert text in entry["snippet"], entry["snippet"]
    assert text.replace("/", "-") in entry["name"], entry["name"]      # a file name has each '/' as '-'


@pytest.mark.parametrize("text,secret", [
    # A command example still loses a secret that a remaining rule finds: a header, a URL password,
    # a secret parameter, a structured "--...password=" key.
    ("curl -H 'Authorization: Bearer SECRETX1' https://x", "SECRETX1"),
    ('curl -H "Authorization: Bearer SECRETX1" https://x', "SECRETX1"),
    ("curl --oauth2-bearer 'ya29.a0AfH6SMBx9Qk2' https://x", "a0AfH6SMBx9Qk2"),
    ("curl https://admin:SECRETX1@kms.example.com/x", "SECRETX1"),
    ("curl -d 'access_token=SECRETX1' https://x", "SECRETX1"),
    ("curl https://x/api?id=1&api_key=SECRETX1", "SECRETX1"),
    ("curl -H 'X-Api-Key: SECRETX1' https://x", "SECRETX1"),
    ("wget --password=SECRETX1 https://x", "SECRETX1"),
])
def test_search_redacts_credentials_a_remaining_rule_finds_in_command_examples(kms, text, secret):
    entry = _search_one(kms, text)
    for field in ("name", "snippet"):
        assert secret not in entry[field], (field, entry[field])
        assert entry[field].startswith("Guide " + text.split()[0] + " "), (field, entry[field])


@pytest.mark.parametrize("text", ["rsync -u server:/data ./backup", "rsync -u backup01:/data/ ./data",
                                  'sqlmap -u "http://target/?id=1"', "ffuf -u https://target/FUZZ"])
def test_search_keeps_a_command_line_host_path_or_url(kms, text):
    # "-u" before a "host:/path" or a URL: the part after the ':' is no password.
    entry = _search_one(kms, text)
    assert text in entry["snippet"], entry["snippet"]
    assert text.replace("/", "-") in entry["name"], entry["name"]      # a file name has each '/' as '-'


@pytest.mark.parametrize("credential", ["dXNlcjpwYXNz", "cmVhZGVyOmFkbWlu", "dXNlcjpMaWdodGhvdXNl"])
def test_search_redacts_a_letters_only_basic_credential(kms, credential):
    # Base64 of "user:password" may hold no digit, '+', '/' or '='.
    entry = _search_one(kms, f"Authorization: Basic {credential} then Basic SuccessFactors setup")
    for field in ("name", "snippet"):
        assert credential not in entry[field], (field, entry[field])
        assert "Basic SuccessFactors setup" in entry[field], (field, entry[field])


def test_search_scrub_keeps_harmless_parameters(kms):
    entry = _search_one(kms, _URL + "?id=7&lang=en&R&D")
    assert "id=7" in entry["snippet"] and "lang=en" in entry["snippet"] and "R&D" in entry["snippet"]
    # Dropping the first parameter keeps the URL's "?"; a plain word after "Bearer" stays.
    entry = _search_one(kms, _URL + "?token=Sv1x9Qk2&lang=en uses Bearer authentication")
    assert _URL + "?lang=en" in entry["snippet"] and "Bearer authentication" in entry["snippet"]


@pytest.mark.parametrize("text", ["Keys, codes and sessions", "Q&A: code=red", "Basic troubleshooting steps",
                                  "The password policy and token rotation", "Use HTTP Basic authentication",
                                  "Price < 100 NIS", "Orders <100 get free shipping",
                                  "Password: reset steps", "Token: what it is", "Top secret: do not share",
                                  "Session: Monday", "Ticket: open one with support",
                                  # Authored prose is shown as written, secrets included (user decision A).
                                  "Password: 123456", "pwd: 1234", "Temporary password: abc123",
                                  "Ticket: INC0012345", "Ticket: #12345678", "Session: 2026-11-04",
                                  "Session: 09:30-11:00 in room B", 'Ticket: "Billing question"',
                                  'Secret: "Do not share"', "PASSWORD: Reset-2026 steps",
                                  "Bearer token-based authentication", "Basic SuccessFactors setup",
                                  "Basic Authentication setup",
                                  # JSON keys that are not credential names (only URL parameters use them).
                                  '{"code": "E1001", "message": "Card declined"}', '{"error": {"code": 404}}',
                                  '{"key": "Enter"}', '{"auth": "oauth2"}', '{"signature": "John Smith"}',
                                  '{"ticket": "INC0012345"}',
                                  # A quoted plain word is a key only where a key starts, and then only
                                  # a quoted value goes.
                                  'Fields: "Username": your ID, "Password": your PIN',
                                  'Open the "Token": section of the portal', "Label 'Session': Monday 09:30",
                                  '"code": 1234 is the store code', 'In the field "Password": type the PIN',
                                  'Click "Password": "Reset" to start',
                                  # Labels that are not identifier-shaped.
                                  "Top-Secret: internal use only", "One-Time-Password: sent by SMS",
                                  "One-time-password: sent by SMS", "Forgot-password: click the link",
                                  "Reset-Password: Open Settings", "Change-password: Go to Settings",
                                  "SafeNet eToken: insert the device", "ws-security-token: see section 3",
                                  "Re-token: COVID-19", "e.g.password: 1234",
                                  # A sentence boundary without a space is not a dotted config key.
                                  "Reset it.Password: abc123", "Done.Token: see the portal", "No.Password: 1234",
                                  "Chapter 12.Token: what it is", "Go to Settings.Password: Open the tab",
                                  # "pass" names a secret only at the end of a compound machine name.
                                  "Pass: the exam", "Boarding-Pass: gate 4", "bypass: see section 2",
                                  "by-pass: see section 2", "BYPASS: see section 2", "PASS: 80 points",
                                  # A name ending with "pass" after a word that names no service.
                                  "LastPass: Open the vault", "KeePass: download the app", "PayPass: tap the card",
                                  "MetroPass: 49 NIS a month", "boarding_pass: gate 4", "boardingPass: gate 4",
                                  "first_pass: done", "two_pass=true", "isPass: true", "low_pass: 200 Hz",
                                  "renderPass: shadow", '{"dayPass": "included"}', '{"boardingPass": "BP123"}',
                                  '{"Boarding-Pass": "Gate 4"}', "User-Pass: valid 30 days", "Admin-Pass: see IT",
                                  # "-u" without a user:password pair, and a numeric user:group pair.
                                  "ls -u", "sort -u file", "pip install --user requests",
                                  "docker run -u 1000:1000 app", "docker run -u 1000:1000",
                                  "journalctl -u nginx", "systemctl status -u",
                                  # "-u" before a time format, a host path, a shell substitution or a URL.
                                  "date -u +%Y-%m-%dT%H:%M:%SZ", "date -u +%H:%M",
                                  'docker run -u "$(id -u):$(id -g)" app',
                                  "docker run --user $(id -u):$(id -g) app", "chown -R $(id -u):$(id -g) dir",
                                  # A unit name and a colon in prose: a key-only "-u KEY:" holds a digit.
                                  "Run journalctl -u nginx: it shows the log",
                                  # Documented limitation: an all-caps single-word key is shown as written.
                                  "PGPASSWORD=Sv1x9Qk2"])
def test_search_scrub_keeps_ordinary_prose(kms, text):
    entry = _search_one(kms, text)
    assert text in entry["snippet"], entry["snippet"]
    assert text in entry["name"], entry["name"]


@pytest.mark.parametrize("text,shown", [
    (f"api_key: {_H}.{_P}.{_S} next", "api_key: [redacted] next"),
    (f'{{"apiToken": {_H}.{_P}.{_S}, "lang": "en"}}', '{"apiToken": [redacted], "lang": "en"}'),
    ("db_password=https://u:Sv1x9Qk2@db.example.com/x next", "db_password=[redacted] next"),
    ("X-Auth-Token: Bearer Sv1x9Qk2abc next", "X-Auth-Token: [redacted] next"),
    ("api_key: [Sv1x9Qk2] next", "api_key: [redacted] next"),
    # Escaped JSON keeps its escaping and every other key and value.
    ('"body": "{\\"username\\":\\"u\\",\\"password\\":\\"Sv1x9Qk2\\"}"',
     '"body": "{\\"username\\":\\"u\\",\\"password\\":\\"[redacted]\\"}"'),
])
def test_search_redaction_leaves_no_debris(kms, text, shown):
    # A value already redacted by an earlier rule is not redacted again, and no bracket is left over.
    entry = _search_one(kms, text)
    for field in ("name", "snippet"):
        assert shown in entry[field], (field, entry[field])
        assert "]]" not in entry[field] and "Sv1x9Qk2" not in entry[field], (field, entry[field])


@pytest.mark.parametrize("text", ["Set API_TOKEN=token", "X-Auth-Token: Bearer", "X-Auth-Token: Bearer\nnext",
                                  "Set X-Auth-Token: Bearer, then the token", "x-api-key: Basic;",
                                  "auth_token: token", '{"apiToken":Token}', "client_secret=token&lang=en",
                                  "api_key: token;x", "api_key: Basic}"])
def test_search_keeps_a_scheme_word_with_no_credential_after_it(kms, text):
    # A scheme word that ends the text, a line or the value carries no credential: the search
    # succeeds (it once failed with a server error) and the text is shown as written. The text ends
    # both fields, so no word follows that could pass for a credential.
    raw_id = str(uuid.uuid4().int)[:9]
    kms["mock"].STATE["extra_results"] = [_record(raw_id, "Guide " + text, description="Guide " + text)]
    entry = _by_id(client(kms).search_files("x"))[f"item:{raw_id}"]
    shown = " ".join(text.split())
    for field in ("name", "snippet"):
        assert shown in entry[field], (field, entry[field])
        assert "[redacted]" not in entry[field], (field, entry[field])


@pytest.mark.parametrize("text", ["x <= 5 and y >= 3", "Orders < 100 and > 50 NIS", "5 < 6 > 4",
                                  "Discount: <50% off> today", "?page=1&section=2&times=3",
                                  "?page=1&amplitude=5", "Q&A &notifications", "R&D &copy2026 &notin"])
def test_search_keeps_comparisons_and_bare_ampersands(kms, text):
    # A '<' starts a tag only before a letter, '/', '!' or '?'; an entity needs its ';'.
    entry = _search_one(kms, text)
    assert text in entry["snippet"], entry["snippet"]
    assert text in entry["name"], entry["name"]


def test_search_still_decodes_terminated_entities(kms):
    entry = _search_one(kms, "Q&amp;A &lt;b&gt; &#169; &#xA9; &copy; &notin; &bogus;")
    assert "Q&A <b> © © © ∉ &bogus;" in entry["snippet"], entry["snippet"]


@pytest.mark.parametrize("text,shown", [
    # A heading whose value is on the next line is not a credential.
    ("Password:\nSelf-service reset steps", "Password: Self-service reset steps"),
    ("Token:\nRe-issue it from the portal", "Token: Re-issue it from the portal"),
    # Inline tags between two elements keep the words apart.
    ("<a>Home</a><a>Contact</a>", "Home Contact"),
    ("<b>Step 1</b><i>Open the app</i>", "Step 1 Open the app"),
    ("<b>Note:</b>Use the portal", "Note: Use the portal"),
    # A tag right after ':' or '=' joins only what the author wrote without a space.
    ("Note:<b>Read this</b>", "Note:Read this"),
    ("Note: <b>Read this</b>", "Note: Read this"),
    ("Step=<i>Open the app</i>", "Step=Open the app"),
    ("Step = <i>Open the app</i>", "Step = Open the app"),
    # Inline tags inside one word still join it.
    ("Know<wbr>ledge base", "Knowledge base"),
])
def test_search_scrub_keeps_headings_and_words_apart(kms, text, shown):
    entry = _search_one(kms, text)
    assert shown in entry["snippet"], entry["snippet"]


def test_scrub_keeps_joiners_inside_words_but_not_inside_tokens(kms):
    # Pure: _scrub also runs on whole article bodies, where a joiner shapes a word or an emoji.
    # A variation selector right after an emoji or an ideograph shapes it, so it stays.
    for word in ("\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645", "\U0001F468\u200d\U0001F469\u200d\U0001F467",
                 "\u2764\ufe0f", "\U0001F3F3\ufe0f\u200d\U0001F308", "\u845b\U000E0100"):
        assert kc._scrub("see " + word + " here") == "see " + word + " here"
    secret = "Sv" + uuid.uuid4().hex[:8] + "9Q"
    for text in (f"{_URL}?to\u200dken={secret}", f"{_URL}?token\u200c\u200c={secret}",
                 f"{_H[:8]}\u200d{_H[8:]}.{_P}.{_S}"):
        entry = _search_one(kms, text)
        for field in ("name", "snippet"):
            assert secret not in entry[field] and _H[8:] not in entry[field], (text, entry[field])


# ------------------------------------------------------------------ scrub: regression corpus
_SV = "Sv1x9Qk2"                          # the secret of every must-redact phrase that holds one value
_JWT_PIECES = (_P[3:], _S)                # what must go from a JWT (its header is no secret)
_SECRET_PARAM_NAMES = ("id_token", "refresh_token", "jwt", "auth", "password", "passwd", "pwd", "api_key", "apikey",
                       "key", "secret", "sig", "signature", "code", "jsessionid", "session", "sessionid",
                       "X-Amz-Security-Token", "X-Amz-Signature", "X-Amz-Credential", "client_secret",
                       "auth_token", "private_token", "access_key", "ticket", "newPassword", "apiToken",
                       "refreshToken", "secret_key", "auth_key", "private_key", "aws_secret_access_key")
_UPPER_PASS_NAMES = ("ADMIN_PASS", "USER_PASS", "POSTGRES_PASS", "PG_PASS", "MONGO_PASS", "RABBITMQ_PASS",
                     "AMQP_PASS", "EMAIL_PASS", "SSH_PASS", "VPN_PASS", "APP_PASS", "API_PASS", "KEYSTORE_PASS",
                     "KEY_PASS", "SA_PASS", "SQL_PASS", "DATABASE_PASS", "DB2_PASS", "REDIS2_PASS",
                     "LDAP_BIND_PASS", "SMTP_AUTH_PASS", "DB_ADMIN_PASS")


def _redact(source, text, secret=_SV, shown=""):
    """A must-redact phrase: every piece of `secret` is gone; `shown` (if any) is in the name and the snippet."""
    return source, text, (secret,) if isinstance(secret, str) else tuple(secret), shown


def _keep(source, text, shown=None):
    """A must-keep phrase: shown as written (blank runs and line breaks read as one space)."""
    return source, text, (), " ".join(text.split()) if shown is None else shown


def _cli_dropped(source, text, shown=None):
    """A former command-line must-redact, now a must-keep: CLI dropped (decision A, 2026-11-05)."""
    return _keep(source + "-cli-dropped", text, shown)


# Every must-redact and must-keep phrase of fix rounds 1-9, by source: rN = .cc10x/p3-fixes-N.md
# (r1 = p3-fixes.md), rvN = the round-N review file, plus the forms of the existing test corpora.
# Later user decisions win over earlier phrases: decision A (round 4) made authored prose a keep, and
# decision A of round 9 made credentials in command examples authored text ("-cli-dropped" ids).
_REGRESSION_CORPUS = [
    # r1: fragments, ';', name spellings, repeated "&amp;", nested URLs, "[]" and an encoded '='.
    _redact("r1", _URL + f"#access_token={_SV}&token_type=bearer"),
    _redact("r1", _URL + f"?id=7;token={_SV};lang=en", shown="lang=en"),
    _redact("r1", _URL + f"?accessToken={_SV}"),
    _redact("r1", _URL + f"?access-token={_SV}"),
    _redact("r1", _URL + f"?TOKEN={_SV}"),
    _redact("r1", _URL + f"?id=7&amp;amp;amp;token={_SV}"),
    _redact("r1", _URL + f"?next=https%3A%2F%2Fkms.example.com%2Fx%3Ftoken%3D{_SV}"),
    _redact("r1", _URL + f"?token[]={_SV}"),
    _redact("r1", _URL + f"?token%3D{_SV}"),
    _redact("r1", f"{_H}­.{_P}.{_S}", _JWT_PIECES),
    _redact("r1", f"{_H}.{_P}​.{_S}", _JWT_PIECES),
    _redact("r1", f"{_H}<wbr>.{_P}.{_S}", _JWT_PIECES),
    _redact("r1", f"{_H}<br>.{_P}.{_S}", _JWT_PIECES),
    _redact("r1", f"Authorization: Bearer {_SV}abc", _SV),
    *[_redact("r1", _URL + f"?id=7&{name}={_SV}&lang=en", shown="lang=en") for name in _SECRET_PARAM_NAMES],
    # r2: every C-category character and tags inside a JWT; tags and breaks after '='; more names.
    _redact("r2", f"{_H}.{_P}.{_S}", _JWT_PIECES),
    _redact("r2", f"{_H}&#xE000;.{_P}.{_S}", _JWT_PIECES),
    _redact("r2", f"{_H}.͸{_P}.{_S}", _JWT_PIECES),
    _redact("r2", f"{_H}.{_P}\ud800.{_S}", _JWT_PIECES),
    _redact("r2", f"{_H[:8]}<wbr>{_H[8:]}.{_P}.{_S}", _JWT_PIECES),
    _redact("r2", f"{_H}.{_P[:9]}<span></span>{_P[9:]}.{_S}", _JWT_PIECES),
    _redact("r2", _URL + f"?token=<br>{_SV}"),
    _redact("r2", _URL + f"?token=<wbr>{_SV}"),
    _redact("r2", _URL + f"?token=\n{_SV}"),
    _redact("r2", _URL + f"?token=<span>{_SV}</span>"),
    _redact("r2", _URL + "?token=Sv1x9Q<wbr>k2tail", ("Sv1x9Q", "k2tail")),
    _redact("r2", _URL + f"?id=1&acc<wbr>ess_token={_SV}"),
    _redact("r2", f"https://user:{_SV}@kms.example.com/x", shown="https://user:[redacted]@kms.example.com"),
    _redact("r2", "Authorization: Basic U3YxeDlRazI6YWJj", "U3YxeDlRazI6YWJj"),
    _redact("r2", "send Basic U3YxeDlRazI6YWJj as the header", "U3YxeDlRazI6YWJj", shown="as the header"),
    _redact("r2", _URL + f"?token&amp;#61;{_SV}"),
    _redact("r2", _URL + f"?id=1\\u0026token={_SV}"),
    _redact("r2", _URL + f"?token='{_SV}'"),
    _redact("r2", _URL + f'?token="{_SV}"'),
    _redact("r2", _URL + f"?token= {_SV}"),
    _redact("r2", _URL + "?next=" + _nested(f"https://kms.example.com/x?token={_SV}", 7)),
    _redact("r2", f"api_key={_SV}"),
    _keep("r2", "Keys, codes and sessions"),
    _keep("r2", "Q&A: code=red"),
    # r3: a '<' that starts no tag, empty values before a secret name, words kept apart, JSON keys.
    _keep("r3", "Price < 100 NIS"),
    _keep("r3", "Orders <100 get free shipping"),
    _redact("r3", _URL + f"?q= &token= {_SV}"),
    _redact("r3", _URL + f"?lang= &password= {_SV}"),
    _redact("r3", _URL + f"?token= ?token= {_SV}"),
    _redact("r3", _URL + f'?a="x ?token= {_SV} "'),
    _keep("r3", "<a>Home</a><a>Contact</a>", "Home Contact"),
    _keep("r3", "<b>Step 1</b><i>Open the app</i>", "Step 1 Open the app"),
    _keep("r3", "Know<wbr>ledge base", "Knowledge base"),
    _redact("r3", f"{_H}.{_P[:9]}<code></code>{_P[9:]}.{_S}", _JWT_PIECES),
    _redact("r3", f"{_H}.{_P[:9]}<kbd></kbd>{_P[9:]}.{_S}", _JWT_PIECES),
    _redact("r3", f'{{"password": "{_SV}"}}'),
    _redact("r3", f'"api_key":"{_SV}"'),
    *[_redact("r3", f"{_H[:8]}{chr(cp)}{_H[8:]}.{_P}.{_S}", _JWT_PIECES)
      for cp in (0x034F, 0x115F, 0x17B4, 0x180B, 0x3164, 0xFE00, 0xFFA0, 0xE0100)],
    _redact("r3", _URL + f"?token='{_SV} and the rest of the guide", shown="and the rest of the guide"),
    _redact("r3", f'api_key: "{_SV} and the rest of the guide', shown="and the rest of the guide"),
    _redact("r3", '{"password": "Sv1x9 Qk2xy"}', ("Sv1x9", "Qk2xy")),
    _redact("r3", _URL + f"?token&amp;equals;{_SV}"),
    _redact("r3", "https://user:Sv1x9@Qk2xy@kms.example.com/x", ("Sv1x9", "Qk2xy")),
    _redact("r3", f"7https://user:{_SV}@kms.example.com/x"),
    _redact("r3", f"1.https://user:{_SV}@kms.example.com/x"),
    _redact("r3", f"--https://user:{_SV}@kms.example.com/x"),
    _keep("r3", "Password: reset steps"),
    _keep("r3", "Token: what it is"),
    _keep("r3", "Top secret: do not share"),
    _keep("r3", "Password:\nSelf-service reset steps"),
    _keep("r3", "Token:\nRe-issue it from the portal"),
    # rv3 (prose phrases a round-3 review wanted redacted are keeps since decision A), tail words.
    _keep("rv3", "Ticket: INC0012345"),
    _keep("rv3", "Ticket: #12345678"),
    _keep("rv3", "Session: 2026-11-04"),
    _keep("rv3", "Session: 10:00-12:00"),
    _keep("rv3", 'Ticket: "Printer not working"'),
    _keep("rv3", 'Token: "what it is"'),
    _keep("rv3", "Username: admin Password: admin"),
    _redact("rv3", f"Authorization: Bearer {_SV} Follow-up", shown="Follow-up"),
    _redact("rv3", f"Authorization: Bearer {_SV} SharePoint site", shown="SharePoint site"),
    # r4: decision A keeps (authored prose), words after a redaction, compound names, '<' and '&' text.
    _keep("r4", "Password: 123456"),
    _keep("r4", "pwd: 1234"),
    _keep("r4", "Temporary password: abc123"),
    _keep("r4", "Session: 09:30-11:00 in room B"),
    _keep("r4", 'Ticket: "Billing question"'),
    _keep("r4", 'Secret: "Do not share"'),
    _keep("r4", "Bearer token-based authentication"),
    _keep("r4", "Basic SuccessFactors setup"),
    _keep("r4", "Basic troubleshooting steps"),
    _keep("r4", "Use HTTP Basic authentication"),
    _keep("r4", "The password policy and token rotation"),
    _keep("r4", "Session: Monday"),
    _keep("r4", "Ticket: open one with support"),
    _keep("r4", "PASSWORD: Reset-2026 steps"),
    _keep("r4", f"password={_SV}"),
    _redact("r4", _URL + f"?token={_SV} COVID-19 guide", shown="COVID-19 guide"),
    _redact("r4", f"{_H}.{_P}.{_S} PowerShell steps", _JWT_PIECES, shown="PowerShell steps"),
    _redact("r4", f"Authorization: Bearer {_SV} Office365 login", shown="Office365 login"),
    _redact("r4", f'"api_key": "{_SV}" iPhone15 app', shown="iPhone15 app"),
    _redact("r4", _URL + f"?id=7&token={_SV}\nFollow-up steps", shown="Follow-up steps"),
    _redact("r4", f"apiToken: {_SV} Self-service portal", shown="Self-service portal"),
    _redact("r4", "Authorization: Basic U3YxeDlRazI6YWJj 2026-11-04 rollout", "U3YxeDlRazI6YWJj",
            shown="2026-11-04 rollout"),
    _redact("r4", f"db_password: {_SV}abc\nKnowledge base", shown="Knowledge base"),
    _redact("r4", f'{{"newPassword": "{_SV}"}}'),
    _redact("r4", f"{{'api_key': '{_SV}'}}"),
    _redact("r4", f"aws_secret_access_key = {_SV}"),
    _redact("r4", f"clientSecret: {_SV}"),
    _redact("r4", f"secret_key = {_SV}"),
    _redact("r4", f"private_key: {_SV}"),
    _keep("r4", "x <= 5 and y >= 3"),
    _keep("r4", "Orders < 100 and > 50 NIS"),
    _keep("r4", "5 < 6 > 4"),
    _keep("r4", "Discount: <50% off> today"),
    _keep("r4", "?page=1&section=2&times=3"),
    _keep("r4", "Q&A &notifications"),
    _keep("r4", "Q&amp;A &lt;b&gt; &#169; &copy; &bogus;", "Q&A <b> © © &bogus;"),
    # rv4: a scheme word as a structured value, quoted keys that name no credential, hyphen labels.
    _redact("rv4", f"X-Auth-Token: Bearer {_SV}abc next", shown="X-Auth-Token: [redacted] next"),
    _redact("rv4", "x-api-key: Basic U3YxeDlRazI6YWJj next", "U3YxeDlRazI6YWJj", shown="next"),
    _keep("rv4", '{"code": "E1001", "message": "Card declined"}'),
    _keep("rv4", '{"error": {"code": 404}}'),
    _keep("rv4", '{"key": "Enter"}'),
    _keep("rv4", '{"auth": "oauth2"}'),
    _keep("rv4", '{"signature": "John Smith"}'),
    _keep("rv4", '{"ticket": "INC0012345"}'),
    _keep("rv4", 'In the field "Password": type the PIN'),
    _keep("rv4", "Top-Secret: internal use only"),
    _keep("rv4", "One-Time-Password: sent by SMS"),
    _keep("rv4", "eToken: insert it"),
    _redact("rv4", _URL + f"?token&#61{_SV}"),
    _redact("rv4", _URL + f"?token&#x3d{_SV}"),
    # r5: quoted keys where a key starts, the shape gate, smart quotes, entities, value stops.
    _redact("r5", f"Authorization: Bearer {_SV}"),
    _redact("r5", f"x-api-key: Basic {_SV}abc", _SV),
    _redact("r5", f'{{"apiToken":{_SV},"lang":"en"}}', shown='"lang":"en"'),
    _redact("r5", f'{{"api_key":"{_SV}"}}'),
    _redact("r5", f'{{"lang": "en", "password": "{_SV}"}}', shown='"lang": "en"'),
    _redact("r5", f"config:\n  'secret': '{_SV}'"),
    _keep("r5", 'Fields: "Username": your ID, "Password": your PIN'),
    _keep("r5", 'Open the "Token": section of the portal'),
    _keep("r5", "Label 'Session': Monday 09:30"),
    _keep("r5", '"code": 1234 is the store code'),
    _keep("r5", 'Click "Password": "Reset" to start'),
    _redact("r5", f"APIKey: {_SV}"),
    _redact("r5", f"APIToken: {_SV}"),
    _redact("r5", f"APISecret: {_SV}"),
    _redact("r5", f"spring.datasource.password={_SV}"),
    _redact("r5", f"db.password={_SV}"),
    _redact("r5", f"run it with --password={_SV} once", shown="once"),
    _redact("r5", f"run it with --auth-token={_SV} once", shown="once"),
    _redact("r5", f"x-api-key: {_SV}"),
    _redact("r5", f"X-Api-Key: {_SV}"),
    _redact("r5", f"X-Auth-Token: {_SV}"),
    _redact("r5", f"X-Amz-Security-Token: {_SV}"),
    _redact("r5", f"client-secret: {_SV}"),
    _redact("r5", f"session_id: {_SV}"),
    _keep("r5", "One-time-password: sent by SMS"),
    _keep("r5", "Forgot-password: click the link"),
    _keep("r5", "Reset-Password: Open Settings"),
    _keep("r5", "Change-password: Go to Settings"),
    _keep("r5", "SafeNet eToken: insert the device"),
    _keep("r5", "ws-security-token: see section 3"),
    _keep("r5", "Re-token: COVID-19"),
    _keep("r5", "e.g.password: 1234"),
    _redact("r5", f"“api_key”: “{_SV}”"),
    _redact("r5", f"{{‘password’: ‘{_SV}’}}"),
    _redact("r5", _URL + f"?id=1&ampsig={_SV}"),
    _redact("r5", _URL + f"?id=1&#38sig={_SV}"),
    _keep("r5", "?page=1&amplitude=5"),
    _keep("r5", "R&D &copy2026 &notin"),
    _redact("r5", f"client_secret={_SV}&lang=en done", shown="&lang=en done"),
    _redact("r5", f"api_key: {_SV};lang=en", shown=";lang=en"),
    _redact("r5", f"api_key: [{_SV}] next", shown="api_key: [redacted] next"),
    _redact("r5", f"MYSQL_PWD={_SV}"),
    _keep("r5", f"PGPASSWORD={_SV}"),            # documented: an all-caps single-word key
    # rv5: a scheme word with no credential after it (once a crash), dotted sentence boundaries.
    _keep("rv5", "X-Auth-Token: Bearer"),
    _keep("rv5", "auth_token: token\nnext"),
    _keep("rv5", "Set API_TOKEN=token"),
    _keep("rv5", '{"auth_token": Bearer}'),
    _keep("rv5", "x-api-key: Basic, next"),
    _keep("rv5", "api_key: token;x"),
    _keep("rv5", "Reset it.Password: abc123"),
    _keep("rv5", "Done.Token: see the portal"),
    _keep("rv5", "No.Password: 1234"),
    _redact("rv5", f"curl -H 'X-Api-Key: {_SV}' https://kms.example.com/x"),
    # r6: more scheme-word keeps, escaped JSON, unpaired quotes, curl, "pass" names, subscription keys.
    _keep("r6", "Set X-Auth-Token: Bearer, then the token"),
    _keep("r6", "x-api-key: Basic;"),
    _keep("r6", '{"apiToken":Token}'),
    _keep("r6", "client_secret=token&lang=en"),
    _keep("r6", "Chapter 12.Token: what it is"),
    _keep("r6", "Go to Settings.Password: Open the tab"),
    _redact("r6", f"app.apiKey: {_SV}"),
    _redact("r6", f'"body": "{{\\"username\\":\\"u\\",\\"password\\":\\"{_SV}\\"}}"',
            shown='\\"username\\":\\"u\\"'),
    _redact("r6", f'{{\\"api_key\\":\\"{_SV}\\"}}'),
    _redact("r6", f'{{\\"client_secret\\": \\"{_SV}\\"}}'),
    _redact("r6", f'“api_key": "{_SV}"'),
    _redact("r6", f'"api_key”: "{_SV}"'),
    _redact("r6", f'{{“password": "{_SV}"}}'),
    _cli_dropped("r6", f"curl -u admin:{_SV} x"),
    _cli_dropped("r6", f"curl --user admin:{_SV} x"),
    _cli_dropped("r6", f"curl --user=admin:{_SV} x"),
    _cli_dropped("r6", f"curl -uadmin:{_SV} x"),
    _cli_dropped("r6", f"curl -u 'admin:{_SV}' x"),
    _keep("r6", "ls -u"),
    _keep("r6", "sort -u file"),
    _redact("r6", f"DB_PASS={_SV}"),
    _redact("r6", f"SMTP_PASS: {_SV}"),
    _redact("r6", f"db-pass: {_SV}"),
    _redact("r6", f"dbPass: {_SV}"),
    _redact("r6", f"Ocp-Apim-Subscription-Key: {_SV}"),
    _redact("r6", f"subscription-key: {_SV}"),
    _keep("r6", "Pass: the exam"),
    _keep("r6", "Boarding-Pass: gate 4"),
    _keep("r6", "bypass: see section 2"),
    _keep("r6", "by-pass: see section 2"),
    _keep("r6", "BYPASS: see section 2"),
    _keep("r6", "PASS: 80 points"),
    # rv6: command-line idioms that are no credential; a quoted password on its own.
    _keep("rv6", "date -u +%Y-%m-%dT%H:%M:%SZ"),
    _keep("rv6", 'docker run -u "$(id -u):$(id -g)" app'),
    _keep("rv6", "rsync -u backup01:/data/ ./data"),
    _keep("rv6", 'sqlmap -u "http://target/?id=1"'),
    _keep("rv6", "ffuf -u https://target/FUZZ"),
    _cli_dropped("rv6", 'curl -u admin:"S3cr3t99" x'),
    # r7: the curl forms, and the "pass" names that name no service.
    _cli_dropped("r7", f"curl -su admin:{_SV} x"),
    _cli_dropped("r7", f"curl -sku admin:{_SV} x"),
    _cli_dropped("r7", f"curl -vu admin:{_SV} x"),
    _cli_dropped("r7", "curl https://api.stripe.com/v1/charges -u sk_demo_4eC39HqLyjWDarjtT1zdp7dc:"),
    _cli_dropped("r7", "curl -u sk_live_X1:"),
    _cli_dropped("r7", "curl -u KEY12345: -d a=b"),
    _cli_dropped("r7", f"curl -u admin:'{_SV}' x"),
    _cli_dropped("r7", f'curl -u admin:"{_SV}" x'),
    _cli_dropped("r7", "curl -u admin:'Sv1x9 Qk2xy' x"),
    _cli_dropped("r7", f"curl -U proxy:{_SV} x"),
    _cli_dropped("r7", f"curl --proxy-user proxy:{_SV} x"),
    _cli_dropped("r7", f"curl --proxy-user=proxy:{_SV} x"),
    _cli_dropped("r7", f'curl -u "admin:{_SV}" x'),
    _cli_dropped("r7", f"curl --user=reader:{_SV} -X GET"),
    _redact("r7", f"SMTP_PASS={_SV}"),
    _redact("r7", f"smtpPass={_SV}"),
    _redact("r7", f"smtp-pass: {_SV}"),
    _redact("r7", f"MYSQL_ROOT_PASS={_SV}"),
    _redact("r7", f"REDIS_PASS={_SV}"),
    _keep("r7", "date -u +%H:%M"),
    _keep("r7", "rsync -u server:/data ./backup"),
    _keep("r7", "docker run --user $(id -u):$(id -g) app"),
    _keep("r7", "chown -R $(id -u):$(id -g) dir"),
    _keep("r7", "docker run -u 1000:1000"),
    _keep("r7", "docker run -u 1000:1000 app"),
    _keep("r7", "journalctl -u nginx"),
    _keep("r7", "Run journalctl -u nginx: it shows the log"),
    _keep("r7", "systemctl status -u"),
    _keep("r7", "pip install --user requests"),
    _keep("r7", "LastPass: Open the vault"),
    _keep("r7", "KeePass: download the app"),
    _keep("r7", "PayPass: tap the card"),
    _keep("r7", "MetroPass: 49 NIS a month"),
    _keep("r7", "boarding_pass: gate 4"),
    _keep("r7", "boardingPass: gate 4"),
    _keep("r7", "first_pass: done"),
    _keep("r7", "two_pass=true"),
    _keep("r7", "isPass: true"),
    _keep("r7", "low_pass: 200 Hz"),
    _keep("r7", "renderPass: shadow"),
    _keep("r7", '{"dayPass": "included"}'),
    _keep("r7", '{"boardingPass": "BP123"}'),
    _keep("r7", '{"Boarding-Pass": "Gate 4"}'),
    _keep("r7", "User-Pass: valid 30 days"),
    _keep("r7", "Admin-Pass: see IT"),
    # rv7: the "pass" and NTLM regressions of round 7 (their fixes are round 8).
    *[_redact("rv7", f"{name}={_SV}") for name in ("ADMIN_PASS", "USER_PASS", "POSTGRES_PASS", "PG_PASS",
                                                    "MONGO_PASS", "RABBITMQ_PASS", "EMAIL_PASS", "SSH_PASS",
                                                    "VPN_PASS", "APP_PASS", "API_PASS", "KEYSTORE_PASS",
                                                    "SA_PASS", "SQL_PASS", "LDAP_BIND_PASS")],
    _redact("rv7", f"WIFI_PASS: {_SV}"),
    _redact("rv7", f"adminPass: {_SV}"),
    _redact("rv7", f'{{"adminPass": "{_SV}"}}'),
    _redact("rv7", f"emailPass={_SV}"),
    _cli_dropped("rv7", "curl --ntlm -u 'CORP\\jdoe:Winter2026!' https://sp.example/x"),
    _cli_dropped("rv7", f"curl -u CORP\\\\jdoe:{_SV} x"),
    _cli_dropped("rv7", f'curl --user "CORP\\\\jdoe:{_SV}" x'),
    _cli_dropped("rv7", "curl -u josé:Winter2026 x"),
    # r8 item 1: any upper-case env "..._PASS" key; lower-case names need a service word.
    *[_redact("r8", f"{name}={_SV}") for name in _UPPER_PASS_NAMES],
    _redact("r8", f"postgresPass: {_SV}"),
    _redact("r8", f"ssh_pass: {_SV}"),
    _redact("r8", f"vpn-pass: {_SV}"),
    _redact("r8", f"keystorePass={_SV}"),
    _redact("r8", f"apiPass: {_SV}"),
    _redact("r8", f'{{"emailPass": "{_SV}"}}'),
    # r8 item 2: Windows and accented user names, an empty user, a quoted pair with a space.
    _cli_dropped("r8", "curl --ntlm --user CORP\\\\jdoe:SECRETX1 x"),
    _cli_dropped("r8", 'curl -u "DOMAIN\\\\user:SECRETX1" x'),
    _cli_dropped("r8", "curl -u :SECRETPAT1 https://dev.azure.com/org/_apis"),
    _cli_dropped("r8", 'curl -u ":SECRETPAT1" x'),
    _cli_dropped("r8", 'curl --user ":SECRETPAT1" x'),
    _keep("r8", "curl -u : https://x"),
    _keep("r8", "curl --user : https://x"),
    _cli_dropped("r8", "curl -u 'admin:Pa ss' x"),
    # r8 item 3: "-U" and bundled clusters belong to curl; host paths, Maven goals and times are no pair.
    _keep("r8", "mvn -U spring-boot:run"),
    _keep("r8", "mvn -U dependency:tree"),
    _keep("r8", "mvn -eU versions:display-dependency-updates"),
    _keep("r8", "rsync -avu server:data/ ./data"),
    _keep("r8", "rsync -avu server:backup/ ./local"),
    _keep("r8", "rsync -avzu user@host:backups/ ./"),
    _keep("r8", "rsync -avzu host:~/data ."),
    _keep("r8", "rsync -u host:~/backup ./"),
    _keep("r8", "rclone copy -u gdrive:Backups ./local"),
    _keep("r8", "socat -u UDP-RECV:5000 STDOUT"),
    _keep("r8", "Sun -Thu 09:00-17:00"),
    _keep("r8", "Sun -Thu 8:30, Fri 8:00"),
    # r8 item 4: placeholders, URL values of "pass" names, Title-Hyphen labels.
    _keep("r8", "curl -u username:password https://x"),
    _keep("r8", "curl -u admin:&lt;password&gt; https://x", "curl -u admin:<password> https://x"),
    _keep("r8", "curl -u admin:{password} https://x"),
    _keep("r8", "curl -u admin:[password] https://x"),
    _keep("r8", "curl -u admin:*** https://x"),
    _keep("r8", "curl -u admin:xxxx https://x"),
    _keep("r8", "curl -u admin:.... https://x"),
    _keep("r8", "curl -u admin:changeme https://x"),
    _keep("r8", "curl -u admin:your_password https://x"),
    _keep("r8", "curl -u 'admin:PASSWORD' https://x"),
    _keep("r8", "api_key: &lt;your-key&gt;", "api_key: <your-key>"),
    _keep("r8", '"password": "&lt;password&gt;"', '"password": "<password>"'),
    _keep("r8", "DB_PASSWORD=changeme"),
    _keep("r8", "api_key: xxxx"),
    _keep("r8", "client_secret: {secret}"),
    _keep("r8", '{"api_key": "YourPassword"}'),
    _keep("r8", "proxy_pass: http://backend:8080"),
    _keep("r8", "nginx_proxy_pass: http://backend"),
    _redact("r8", f"proxy_pass: http://u:{_SV}@backend:8080", shown="proxy_pass: http://u:[redacted]@backend"),
    _keep("r8", "Root-Pass: see the vault"),
    _keep("r8", "Mail-Pass: ask IT"),
    _keep("r8", "Proxy-Pass: required"),
    _redact("r8", f"DB-PASS: {_SV}"),
    # r8 item 5: a ';' or ',' after an unquoted password is the shell's or the sentence's.
    _cli_dropped("r8", f"curl -u admin:{_SV}; echo done"),
    _cli_dropped("r8", f"curl -u admin:{_SV}, then run it"),
    # wget: its "--...password=" flags are structured keys, a remaining family, so they still go.
    _redact("r8", f"wget --user=admin --password={_SV} x", shown="wget --user=admin --password=[redacted] x"),
    _redact("r8", f"wget --http-user=a --http-password={_SV} x"),
    _redact("r8", f"wget --proxy-password={_SV} x"),
    _redact("r8", f"wget ftp://admin:{_SV}@host/x"),
    # rv8 (round-8 review): the shape-varied command examples, now shown as written (decision A, round 9).
    _cli_dropped("rv8", "curl -su admin:admin x"),
    _cli_dropped("rv8", "curl -su admin:Welcome x"),
    _cli_dropped("rv8", "curl -U proxy:Winter x"),
    _cli_dropped("rv8", "curl -U proxyuser:Summer-Time x"),
    _cli_dropped("rv8", "curl -u AKIAIOSFODNN7EXAMPLE:wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY https://x"),
    _cli_dropped("rv8", "curl -u admin:Xk9+aB3/q2 x"),
    _cli_dropped("rv8", "curl -X POST \\\n-u admin:Welcome \\\nhttps://x"),
    _cli_dropped("rv8", "curl \\<br>-U proxy:Pr0xy2026 https://x", "curl \\ -U proxy:Pr0xy2026 https://x"),
    _cli_dropped("rv8", "curl \\\n-u ADMIN:123456 https://x"),
    _cli_dropped("rv8", "curl -u jane@example.com:YOUR_API_TOKEN https://x"),
    _keep("rv8", "X-Auth-Token: Bearer\nnext"),                # round 6: a scheme word at a line end (once a crash)
    # r9 item 1: the remaining shapes of the fix list, shown as written, and the families that still apply.
    _cli_dropped("r9", f"wget --password {_SV} x"),
    _cli_dropped("r9", f"wget --user admin --password {_SV} https://x"),
    _cli_dropped("r9", f"curl --user admin:{_SV} --proxy-user proxy:{_SV} https://x"),
    _cli_dropped("r9", f"mysql -u root -p{_SV} db"),
    _cli_dropped("r9", f"curl -x user:{_SV}@proxy:3128 https://x"),
    _redact("r9", f"curl -H 'Authorization: Bearer {_SV}' https://x", shown="Authorization: Bearer [redacted]"),
    _redact("r9", f"curl https://user:{_SV}@kms.example.com/x", shown="curl https://user:[redacted]@kms"),
    _redact("r9", f"curl -d 'access_token={_SV}' https://x", shown="curl -d 'access_token=[redacted]"),
    _redact("r9", f'curl -H "X-Api-Key: {_SV}" https://x'),
    # r9 item 2: a quoted Bearer token; an inline tag between a separator and its value.
    _redact("r9", 'curl --oauth2-bearer "ya29.a0AfH6SMBx9Qk2" https://x', "a0AfH6SMBx9Qk2",
            shown='--oauth2-bearer "[redacted]" https:'),
    _redact("r9", f"curl --oauth2-bearer '{_SV}' https://x", shown="--oauth2-bearer '[redacted]' https:"),
    _redact("r9", f'Authorization: "Bearer {_SV}abcd"', shown='Authorization: "Bearer [redacted]"'),
    _keep("r9", "Bearer token-based authentication"),
    _keep("r9", 'Bearer "token-based" authentication'),
    _redact("r9", f'api_key: <span style="color:red">{_SV}</span>', shown="api_key: [redacted]"),
    # These three pass with or without the tag join (blanks after ':' or '=' are allowed, and "?token= S"
    # is a spaced value); the two userinfo phrases below are the ones that guard the join.
    _redact("r9-tag-spaced", f'api_key:<span style="color:red">{_SV}</span>'),
    _redact("r9-tag-spaced", f'{{"password":<b>"{_SV}"</b>}}'),
    _redact("r9", f"https://admin:<b>{_SV}</b>@host/x", shown="https://admin:[redacted]@host"),
    _redact("r9", f"https://admin:<span>{_SV}</span><span>@host</span>/x", shown="https://admin:[redacted]@host"),
    _redact("r9-tag-spaced", _URL + f"?token=<b>{_SV}</b>&lang=en", shown="lang=en"),
    _keep("r9", "Note:<b>Read this</b>", "Note:Read this"),
    _keep("r9", "Note: <b>Read this</b>", "Note: Read this"),
    _keep("r9", "<b>Note:</b>Use the portal", "Note: Use the portal"),
    _keep("r9", "Mail <b>me</b>@home", "Mail me@home"),     # read as the page shows it
    # r9 item 3: placeholder shapes in a structured value.
    _keep("r9", "api_key: YOUR_API_KEY"),
    _keep("r9", '"apiKey": "YOUR_API_KEY"'),
    _keep("r9", "API_KEY=your_api_key_here"),
    _keep("r9", "api_key: &lt;your api key&gt;", "api_key: <your api key>"),
    _keep("r9", "api_key: [your api key]"),
    _keep("r9", "api_key: {your api key}"),
    _keep("r9", "DB_PASS=${DB_PASS}"),
    _keep("r9", "DB_PASSWORD=$DB_PASSWORD"),
    _keep("r9", "set API_KEY=%API_KEY%"),
    _keep("r9", "client_secret: MY_SECRET"),
    _keep("r9", "auth_token=TOKEN_HERE"),
    _keep("r9", "db_password: PASSWORD"),
    _redact("r9", f"api_key: &lt;{_SV}", shown="api_key: [redacted]"),
    _redact("r9", f"api_key: [{_SV} x]", ("Sv1x9Qk2",)),
    _redact("r9", f"DB_PASS=$({_SV})"),
    _redact("r9", f"api_key: YOUR_{_SV}"),
    # r9 item 4: guards the depth of nested decoding (both fail with _MAX_SCRUB_DEPTH = 0).
    _keep("r9", _URL + "?q=caf%C3%A9&lang=en"),
    _redact("r9", _URL + f"?next=https%3A%2F%2Fkms.example.com%2Fx%3Fid%3D7%26token%3D{_SV}",
            shown="x?id=7"),
    # r10 (P3 round-9 review and hunt, carried into P4): documentation placeholders after a scheme word
    # or as a structured value stay; a placeholder glued to a real value does not hide it.
    _keep("r10", "Authorization: Bearer YOUR_ACCESS_TOKEN"),
    _keep("r10", "X-Auth-Token: Bearer YOUR_TOKEN"),
    _keep("r10", 'Authorization: Bearer "YOUR_ACCESS_TOKEN"'),
    _keep("r10", "Authorization: Bearer ACCESS_TOKEN"),
    _keep("r10", "client_secret: YOUR_CLIENT_SECRET"),
    _keep("r10", "auth_token: YOUR_AUTH_TOKEN"),
    _keep("r10", "api_key: your-api-key"),
    _keep("r10", "x-api-key: YOUR-API-KEY"),
    _keep("r10", "API_KEY=INSERT_KEY_HERE"),
    _keep("r10", "api_key: your-key-here"),
    _keep("r10", '"api_key": "YOUR API KEY"'),
    _redact("r10", f"Authorization: Bearer YOUR_{_SV}"),
    _redact("r10", f"api_key: your-{_SV}"),
    # A smart, escaped or backtick quote may open a Bearer token.
    _redact("r10", "curl --oauth2-bearer “ya29.a0AfH6SMBx9Qk2” https://x", "a0AfH6SMBx9Qk2"),
    _redact("r10", "curl --oauth2-bearer ‘ya29.a0AfH6SMBx9Qk2’ https://x", "a0AfH6SMBx9Qk2"),
    _redact("r10", 'Authorization: Bearer \\"ya29.a0AfH6SMBx9Qk2\\"', "a0AfH6SMBx9Qk2"),
    _redact("r10", "Authorization: Bearer `ya29.a0AfH6SMBx9Qk2`", "a0AfH6SMBx9Qk2"),
    # A placeholder glued to a value is no placeholder.
    _redact("r10", f"api_key: &lt;your api key&gt;{_SV}"),
    _redact("r10", f"DB_PASS=${{DB}}{_SV}"),
    _redact("r10", f"api_key: {{key}}{_SV}"),
    # One sentence mark after a placeholder still leaves a placeholder.
    _keep("r10", "Set api_key: &lt;your api key&gt;.", "Set api_key: <your api key>."),
    _keep("r10", "(api_key: &lt;your api key&gt;)", "(api_key: <your api key>)"),
    _keep("r10", "api_key: YOUR_API_KEY."),
    _keep("r10", "(api_key: YOUR_API_KEY)"),
    _keep("r10", "DB_PASS=${DB_PASS}."),
    _redact("r10", f"api_key: {_SV}."),
    # Template expressions as values.
    _keep("r10", "api_key: ${{ secrets.API_KEY }}"),
    _keep("r10", "client_secret: {{ .Values.clientSecret }}"),
    _keep("r10", '"clientSecret": "{{ .Values.clientSecret }}"'),
    _keep("r10", "DB_PASSWORD=${POSTGRES_PASSWORD:-postgres}"),
    _redact("r10", f"api_key: ${{{{ secrets.API_KEY }}}}{_SV}"),
    # r11 (P4 review C1): a variable's default and a template body that hold a credential are no placeholder;
    # each keep below has its real-value twin next to it, and most phrases go on after the value.
    _keep("r11", "DB_PASSWORD=${POSTGRES_PASSWORD:-postgres} for local dev"),
    _redact("r11", "DB_PASSWORD=${POSTGRES_PASSWORD:-Xk29pqLm7Rt}", "Xk29pqLm7Rt"),
    _redact("r11", "DB_PASSWORD=${POSTGRES_PASSWORD:-Xk29pqLm7Rt} for local dev", "Xk29pqLm7Rt",
            shown="for local dev"),
    _keep("r11", '{"password": "${DB_PASS:-changeme}"} in compose'),
    _redact("r11", '{"password": "${DB_PASS:-Sv1x9Qk2abcd}"} in compose', shown="in compose"),
    _redact("r11", '"password": "${DB_PASS:-Sv1x9Qk2abcd}"'),
    _redact("r11", '"password": "${DB_PASS:-Sv1x9Qk2abcd}" in compose', shown="in compose"),
    _keep("r11", "client_secret: ${CLIENT_SECRET:=none} until set"),
    _redact("r11", "client_secret: ${CLIENT_SECRET:=Sv1x9Qk2abcd}"),
    _redact("r11", "client_secret: ${CLIENT_SECRET:=Sv1x9Qk2abcd} until set", shown="until set"),
    # Rewritten in r12 (router decision H1): a default that is itself a placeholder is kept, as
    # "DB_PASS=my_pass" is; the '_' twin of a real default is in the r12-default entries below.
    _keep("r11-rewritten-r12", "DB_PASS=${DB_PASS:-my_pass}"),
    _keep("r11", '"api_key": "{{ .Values.apiKey }}" in values.yaml'),
    _keep("r11", "api_key: {{ secrets.API_KEY }} from the vault"),
    _keep("r11", '"api_key": "{{ apiKey }}" in the body'),
    _redact("r11", '"api_key": "{{ sk_live_51Hx9Qk2abcd }}"', "51Hx9Qk2abcd"),
    _redact("r11", '"api_key": "{{ sk_live_51Hx9Qk2abcd }}" in the body', "51Hx9Qk2abcd", shown="in the body"),
    _redact("r11", "api_key: {{ sk_live_51Hx9Qk2abcd }} in the body", "51Hx9Qk2abcd", shown="in the body"),
    _keep("r11", "api_key: {{apiKey}} per request"),
    _redact("r11", "api_key: {{Sv1x9Qk2abcdEf}}"),
    _redact("r11", "api_key: {{Sv1x9Qk2abcdEf}} per request", shown="per request"),
    _redact("r11", "api_key: {{ .Values.9Qk2abcd }} per request", "9Qk2abcd", shown="per request"),
    # r11 (P4 hunt H1): a placeholder before a closing quote, wrapped in quotes with text after it (below).
    _keep("r11", "api_key: YOUR_API_KEY.) and more"),
    _redact("r11", f"api_key: {_SV}.) and more", shown="and more"),
    _keep("r11", "Use `API_KEY=your_api_key_here` in .env"),
    _redact("r11", f"Use `API_KEY={_SV}` in .env", shown="in .env"),
    # r11 (P4 review, deferred list): Bearer placeholders name what goes there; a word that names nothing
    # ("my_token_abcdef") is a token.
    _keep("r11", "Authorization: Bearer AUTH_TOKEN for the call"),
    _keep("r11", "Authorization: Bearer ID_TOKEN"),
    _keep("r11", "Authorization: Bearer BEARER_TOKEN"),
    _keep("r11", "Authorization: Bearer TOKEN_GOES_HERE"),
    _keep("r11", "Authorization: Bearer REPLACE_WITH_TOKEN and send"),
    _keep("r11", "Authorization: Bearer my_token_here"),
    _keep("r11", "Authorization: Bearer YOUR_ACCESS_TOKEN and then call"),
    _redact("r11", "Authorization: Bearer my_token_abcdef", "my_token_abcdef"),
    _redact("r11", "Authorization: Bearer my_token_abcdef and then call", "my_token_abcdef",
            shown="and then call"),
    _redact("r11", f"Authorization: Bearer AUTH_TOKEN{_SV}"),
    _redact("r11", f"Authorization: Bearer REPLACE_WITH_{_SV} and send", shown="and send"),
    _redact("r11", f"Authorization: Bearer TOKEN_GOES_HERE_{_SV}"),
    _keep("r11", "api_key: YOUR_API_KEY_GOES_HERE in the header"),
    _redact("r11", f"api_key: YOUR_API_KEY_GOES_HERE_{_SV} in the header", shown="in the header"),
    _keep("r11", "auth_token: AUTH_TOKEN in the header"),
    _redact("r11", "auth_token: live_token in the header", "live_token", shown="in the header"),
    _keep("r11", "api_key: &lt;your api key&gt; in the header", "api_key: <your api key> in the header"),
    _redact("r11", f"api_key: {_SV} in the header", shown="in the header"),
    # r11 (P4 hunt L1): more opening quotes and '(' before a Bearer token.
    _redact("r11", "Authorization: Bearer ”ya29.a0AfH6SMBx9Qk2” next", "a0AfH6SMBx9Qk2", shown="next"),
    _redact("r11", "Authorization: Bearer „ya29.a0AfH6SMBx9Qk2“ next", "a0AfH6SMBx9Qk2", shown="next"),
    _redact("r11", "Authorization: Bearer «ya29.a0AfH6SMBx9Qk2» next", "a0AfH6SMBx9Qk2", shown="next"),
    _redact("r11", "Authorization: Bearer (ya29.a0AfH6SMBx9Qk2) next", "a0AfH6SMBx9Qk2", shown="next"),
    _keep("r11", "Authorization: Bearer (YOUR_TOKEN) in the header"),
    *[entry for shape, shown in (
        ("X-Api-Key: YOUR_API_KEY", None),
        ("X-Api-Key: &lt;your-api-key&gt;", "X-Api-Key: <your-api-key>"),
        ("x-api-key: ${API_KEY}", None),
        ("X-Api-Key: $API_KEY", None),
        ("X-Api-Key: ***", None),
        ("x-api-key: {{apiKey}}", None),
        ("X-Auth-Token: Bearer YOUR_TOKEN", None),
        ("API_KEY=your_api_key_here", None),
    ) for opener, closer in (("'", "'"), ('"', '"'), ("`", "`"), ("“", "”"), ("‘", "’")) for entry in (
        _keep("r11-quoted", f"curl -H {opener}{shape}{closer} https://x",
              f"curl -H {opener}{shown or shape}{closer} https://x"),
        _keep("r11-quoted", f"Send {opener}{shape}{closer}. Then call",
              f"Send {opener}{shown or shape}{closer}. Then call"),
        # The payload twin: a value glued after the closing quote leaves no placeholder.
        _redact("r11-quoted", f"curl -H {opener}{shape}{closer}{_SV} https://x", shown="https://x"),
    )],
    *[_redact("r11-quoted", f"curl -H {opener}{key}{_SV}{closer} https://x", shown="https://x")
      for key in ("X-Api-Key: ", "x-api-key: ", "X-Auth-Token: Bearer ", "API_KEY=")
      for opener, closer in (("'", "'"), ('"', '"'), ("`", "`"), ("“", "”"), ("‘", "’"))],
    # r12 (P4 review round 2 and re-hunt, .cc10x/p4-fixes-2.md): a variable's default is letters and '-'
    # only, or a placeholder itself (allow-list); each keep has a redact twin for every character class
    # the old shape still admitted, quoted and unquoted, with text after the value.
    _redact("r12", '"password": "${DB_PASS:-Ch@ngeMe!}"', ("Ch@ngeMe", "ngeMe")),
    _redact("r12", '"password": "${DB_PASS:-Ch@ngeMe!}" in compose', ("ngeMe",), shown="in compose"),
    _redact("r12", '{"password": "${DB_PASS:-Ch@ngeMe!}"} in compose', ("ngeMe",), shown="in compose"),
    _redact("r12", "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-Adm!n-Pa$$} in compose", ("Adm!n", "Pa$$"),
            shown="in compose"),
    _redact("r12", "client_secret: ${CLIENT_SECRET:=Qx.Lm-Rt@Kp} until set", ("Qx.Lm", "Rt@Kp"),
            shown="until set"),
    _redact("r12", "DB_PASSWORD=${DB_PASSWORD:-Kx#mQ!pZ@wRvT}", ("mQ!pZ", "wRvT")),
    _redact("r12", "DB_PASSWORD=${DB_PASSWORD:-P@ssword!}", ("P@ssword",)),
    _keep("r12", "DB_PASSWORD=${DB_PASSWORD:-dev-secret} for local dev"),
    _keep("r12", '{"password": "${DB_PASS:-none}"} in compose'),
    _keep("r12", '"db_password": "${DB_PASS:-dev-secret}" for dev'),
    _keep("r12", "OPENAI_API_KEY=${OPENAI_API_KEY:-your_api_key_here} in .env"),
    _keep("r12", '"api_key": "${OPENAI_API_KEY:-YOUR_API_KEY}" in compose'),
    _redact("r12", f"OPENAI_API_KEY=${{OPENAI_API_KEY:-your_{_SV}}} in .env", shown="in .env"),
    *[entry for symbol in "@!#$.~+/=%^*?:|_0" for entry in (
        _redact("r12-default", f"DB_PASS=${{DB_PASS:-Kx{symbol}mQpZwRv}} for dev", ("mQpZwRv",), shown="for dev"),
        _redact("r12-default", f'"db_password": "${{DB_PASS:-Kx{symbol}mQpZwRv}}" for dev', ("mQpZwRv",),
                shown="for dev"),
    )],
    # The characters an unquoted value stops at (a documented limitation unquoted) go whole when quoted.
    *[_redact("r12-default", f'"db_password": "${{DB_PASS:-Kx{symbol}mQpZwRv}}" for dev', ("mQpZwRv",),
              shown="for dev") for symbol in ",;&]} '"],
    # A Bearer placeholder in capitals and '_' with no digit is kept whatever its last word; the last-word
    # rule is for lower or mixed case. Multi-word capital env names ending _TOKEN/_KEY/_SECRET/_PAT are
    # placeholders (2-20 letters per word, up to four words before the suffix).
    *[_keep("r12", f"Authorization: Bearer {value} for the call") for value in (
        "YOUR_PAT", "YOUR_APIKEY", "YOUR_TOKEN_VALUE", "YOUR_CREDENTIALS", "YOUR_SESSION_ID", "SLACK_BOT_TOKEN",
        "OPENAI_API_KEY", "GITHUB_ACCESS_TOKEN")],
    _keep("r12", "api_key: STRIPE_SECRET_KEY in the header"),
    _keep("r12", "auth_token: SLACK_BOT_TOKEN in the header"),
    _keep("r12", "api_key: GITLAB_PAT in the header"),
    _redact("r12", "Authorization: Bearer QWERTYUIOPASDFGHJKLZXCVBNM_TOKEN", "QWERTYUIOPASDFGHJKLZXCVBNM"),
    _redact("r12", "auth_token: QWERTYUIOPASDFGHJKLZXCVBNM_TOKEN in the header", "QWERTYUIOPASDFGHJKLZXCVBNM",
            shown="in the header"),
    _redact("r12", "Authorization: Bearer AB_CD_EF_GH_IJ_TOKEN next", "AB_CD_EF_GH_IJ", shown="next"),
    _redact("r12", "Authorization: Bearer SLACK_B0T_TOKEN next", "SLACK_B0T", shown="next"),
    _redact("r12", "api_key: STRIPE_S3CRET_KEY in the header", "S3CRET", shown="in the header"),
    _redact("r12", "Authorization: Bearer slack_bot_token next", "slack_bot_token", shown="next"),
    _redact("r12", "Authorization: Bearer YOUR_PAT9Qk2 next", "PAT9Qk2", shown="next"),
    _redact("r12", "Authorization: Bearer Your_Pat_Abcdef next", "Pat_Abcdef", shown="next"),
    # Helm: a chain of bare filters after the reference.
    _keep("r12", '"api_key": "{{ .Values.apiKey | quote }}" in values.yaml'),
    _keep("r12", "api_key: {{ .Values.apiKey | b64enc }} in the secret"),
    _keep("r12", "api_key: {{ .Values.apiKey | b64enc | quote }} in the secret"),
    _keep("r12", "client_secret: {{ .Values.clientSecret|quote }}"),
    _redact("r12", '"api_key": "{{ sk_live_51Hx9Qk2abcd | quote }}"', "51Hx9Qk2abcd"),
    _redact("r12", "api_key: {{ sk_live_51Hx9Qk2abcd | quote }} in the secret", "51Hx9Qk2abcd",
            shown="in the secret"),
    _redact("r12", f"api_key: {{{{ .Values.apiKey | {_SV}abcd }}}} in the secret", shown="in the secret"),
    _redact("r12", '"api_key": "{{ .Values.apiKey | Kx@mQpZwRv }}"', "mQpZwRv"),
    _redact("r12", f"api_key: {{{{ .Values.apiKey | quote }}}}{_SV} in the secret", shown="in the secret"),
    # A dotted template path whose segments hold a digit is a reference only under a known root.
    _redact("r12", "api_key: {{ sk_live_51Hx9Qk2abcd.x }} in the body", "51Hx9Qk2abcd", shown="in the body"),
    _redact("r12", '"api_key": "{{ sk_live_51Hx9Qk2abcd.x }}"', "51Hx9Qk2abcd"),
    _redact("r12", f'"api_key": "{{{{ .a.{_SV}abcd }}}}"'),
    _keep("r12", "api_key: ${{ steps.login2.outputs.token }} in the job"),
    _keep("r12", '"api_key": "{{ .Values.apiKey2 }}"'),
    # More Bearer openers, and "Bearer:" with a colon.
    *[_redact("r12", f"Authorization: Bearer {opener}ya29.a0AfH6SMBx9Qk2{closer} next", "a0AfH6SMBx9Qk2",
              shown="next")
      for opener, closer in (("’", "’"), ("‚", "‘"), ("‹", "›"), ("»", "«"), ("[", "]"), ("{", "}"))],
    _redact("r12", "Authorization: Bearer: ya29.a0AfH6SMBx9Qk2 next", "a0AfH6SMBx9Qk2", shown="next"),
    _redact("r12", "Authorization: Bearer:ya29.a0AfH6SMBx9Qk2 next", "a0AfH6SMBx9Qk2", shown="next"),
    _keep("r12", "Authorization: Bearer: YOUR_ACCESS_TOKEN in the header"),
    _keep("r12", "Authorization: Bearer [YOUR_TOKEN] in the header"),
    _keep("r12", "Authorization: Bearer {YOUR_ACCESS_TOKEN} in the header"),
    _keep("r12", "Token type: Bearer: see the guide"),
    # A shell line continuation glued to a placeholder ends it like a sentence mark.
    _keep("r12", 'curl -H "X-Api-Key: YOUR_API_KEY"\\ https://x'),
    _keep("r12", "curl -H 'X-Api-Key: YOUR_API_KEY'\\\n  https://x"),
    _keep("r12", "X-Api-Key: YOUR_API_KEY\\ next"),
    _redact("r12", f'curl -H "X-Api-Key: {_SV}"\\ https://x', shown="https://x"),
    _redact("r12", f"X-Api-Key: {_SV}\\ next", shown="next"),
    _redact("r12", f"X-Api-Key: YOUR_API_KEY\\{_SV} next", shown="next"),
    _redact("r12", "X-Api-Key: YOUR_API_KEY\\9Q next", "\\9Q", shown="next"),
    # r13 (P4 round 3, item 2): the gap after "Bearer" or "Bearer:" stays on its line, so a YAML key on the
    # next line keeps its value; a "bearer:" key with its token indented on the next line still goes.
    _keep("r13", "auth:\n  bearer:\n    token_file: /etc/token"),
    _keep("r13", "authorization:\n  type: Bearer\n  credentials_file: /etc/prom/token"),
    _keep("r13", "auth:\n  bearer:\n    token_file:/etc/token"),
    _redact("r13", "Authorization: Bearer ya29.a0AfH6SMBx9Qk2 next", "a0AfH6SMBx9Qk2", shown="next"),
    _redact("r13", "bearer: ya29.a0AfH6SMBx9Qk2", "a0AfH6SMBx9Qk2", shown="bearer: [redacted]"),
    _redact("r13", "Bearer:\tya29.a0AfH6SMBx9Qk2", "a0AfH6SMBx9Qk2"),
    _redact("r13", "auth:\n  bearer:\n    ya29.a0AfH6SMBx9Qk2", "a0AfH6SMBx9Qk2"),
    _redact("r13", "bearer:\r\n  ya29.a0AfH6SMBx9Qk2 next", "a0AfH6SMBx9Qk2", shown="next"),
    # r14 (P4 round 4, item 2): any blank but a line break is the gap after "Bearer" or "Bearer:", Unicode
    # spaces (en space, thin space, narrow no-break space, ideographic space, "&ensp;") included.
    *[_redact("r14", f"Authorization: Bearer{gap}ya29.a0AfH6SMBx9Qk2 next", "a0AfH6SMBx9Qk2", shown="next")
      for gap in (" ", " ", " ", "　", "&ensp;")],
    *[_redact("r14", f"bearer:{gap}ya29.a0AfH6SMBx9Qk2 next", "a0AfH6SMBx9Qk2", shown="next")
      for gap in (" ", " ", " ", "　", "&ensp;")],
    _keep("r14", "auth:\n  bearer:\n    token_file : /etc/token"),
]


@pytest.mark.parametrize("source,text,secrets,shown", _REGRESSION_CORPUS,
                         ids=[f"{i:03d}-{entry[0]}" for i, entry in enumerate(_REGRESSION_CORPUS)])
def test_search_regression_corpus(kms, source, text, secrets, shown):
    # Every phrase of every fix round, through the public search: a secret never shows, a keep shows as written.
    # The phrase ends the title and the description, or (r11) goes on after the value.
    raw_id = str(uuid.uuid4().int)[:9]
    kms["mock"].STATE["extra_results"] = [_record(raw_id, "Guide " + text, description="Guide " + text)]
    entry = _by_id(client(kms).search_files("x"))[f"item:{raw_id}"]
    for field in ("name", "path", "snippet"):
        for piece in secrets:
            assert piece not in entry[field], (source, field, entry[field])
    if shown:
        for field in ("name", "snippet"):
            want = shown.replace("/", "-") if field == "name" else shown      # a file name has each '/' as '-'
            assert want in entry[field], (source, field, entry[field])


def _adversarial(unit, size=200_000):
    return (unit * (size // len(unit) + 1))[:size]


_ADVERSARIAL = {
    "jwt_headers": _adversarial("eyJ"),
    "jwt_dots": _adversarial("eyJa."),
    "base64url_run": "eyJ" + _adversarial("Ab9_-"),
    "params": _adversarial("?a=b&"),
    "secret_params_spaced": _adversarial("?token= x "),
    "percent_nesting": "?next=" + _adversarial("%25") + "3D",
    "nested_ampersands": _adversarial("?a=%2526"),
    "scheme_runs": _adversarial("a://"),
    "bearer_words": _adversarial("Bearer "),
    "basic_words": _adversarial("Basic "),
    "plain_words": _adversarial("token "),
    "userinfo": "https://" + _adversarial("a:"),
    "joiners": _adversarial("a\u200d"),
    "entities": _adversarial("&amp;#61;"),
    "named_entities": _adversarial("&amp;equals;"),
    "amp_prefixes": "?" + _adversarial("amp;") + "token=x",
    "spaced_empty_values": _adversarial("?q= "),
    "quoted_secret_values": _adversarial("?token='"),
    "unclosed_quotes": _adversarial("?token='" + "a" * 300),
    "plain_label_quotes": _adversarial('password: "'),
    "json_names": _adversarial('"token":'),
    "plain_label_values": _adversarial("token: Ab1 "),
    "redaction_tails": _adversarial("eyJabcd Abcdefgh1 "),
    "userinfo_ats": "https://a:" + _adversarial("@"),
    "digit_schemes": _adversarial("1a"),
    "variation_selectors": _adversarial("a\ufe0f"),
    "emoji_selectors": _adversarial("\u2764\ufe0f"),
    # Unspaced repeats of every name form the scrub still reads.
    "token_equals": _adversarial("token="),
    "camel_token_equals": _adversarial("apiToken="),
    "json_password_names": _adversarial('"password":'),
    "structured_names": _adversarial("a_b:"),
    "structured_secret_names": _adversarial("api_key:"),
    "structured_unclosed_quotes": _adversarial("api_key: '" + "a" * 300),
    "basic_letters": _adversarial("Basic dXNlcjpwYXNz "),
    # The forms added in fix round 5.
    "structured_scheme_values": _adversarial("x_token: Bearer "),
    "structured_redacted_values": _adversarial("api_key: eyJabcdef, "),
    "structured_bracket_values": _adversarial("api_key: [a"),
    "smart_quoted_names": _adversarial("\u201capi_key\u201d:"),
    "smart_unclosed_quotes": _adversarial("\u201capi_key\u201d: \u201c" + "a" * 300),
    "smart_mismatched_quotes": _adversarial("\u201cpassword\u2019: "),
    "quoted_keys_after_blanks": _adversarial(",      \"password\": x "),
    "acronym_camel_names": _adversarial("APIKey:"),
    "dotted_names": _adversarial("db.password="),
    "dotted_runs": _adversarial("a.") + "password=x",
    "hyphen_runs": _adversarial("a-") + "token: x",
    "cli_flags": _adversarial("--password="),
    "unterminated_entities": _adversarial("&#61"),
    "unterminated_entity_names": _adversarial("?token&#61"),
    "bare_amp_names": "?" + _adversarial("amp") + "sig=x",
    "bare_amp_params": _adversarial("&ampsig=x"),
    # The forms added in fix round 6.
    "escaped_json_names": _adversarial('{\\"password\\":\\"'),
    "escaped_json_shaped_names": _adversarial('\\"api_key\\":'),
    "escaped_unclosed_values": _adversarial('\\"api_key\\": \\"' + "a" * 300),
    "curl_user_pairs": _adversarial("curl -u a:b "),
    "curl_flags_without_blanks": _adversarial(".-u"),
    "curl_long_users": _adversarial(" -u" + "a" * 70),
    "curl_user_flags": _adversarial("--user="),
    "curl_blank_runs": _adversarial("-u" + " " * 50),
    "mismatched_quote_names": _adversarial('“api_key": '),
    "mismatched_plain_names": _adversarial('{“password": "'),
    "mismatched_unclosed_values": _adversarial('“api_key": "' + "a" * 300),
    "compound_pass_names": _adversarial("db-pass: "),
    # The forms added in fix round 7.
    "curl_bundled_flags": _adversarial("curl -sku a:b "),
    "curl_bundled_flag_runs": _adversarial(" -abcdefu "),
    "curl_bundled_blank_runs": _adversarial("-sku" + " " * 50),
    "curl_key_only": _adversarial("-u K1: "),
    "curl_key_only_runs": _adversarial("-u K1:"),
    "curl_quoted_passwords": _adversarial("-u a:'b' "),
    "curl_unclosed_passwords": _adversarial("-u a:'" + "b" * 300),
    "curl_double_quoted_users": _adversarial('-u "a:'),
    "curl_proxy_users": _adversarial("--proxy-user=a:b "),
    "curl_long_user_names": _adversarial(" -u " + "a" * 140),
    "camel_pass_names": _adversarial("boardingPass: "),
    "camel_pass_runs": _adversarial("aPass "),
    "service_pass_names": _adversarial("smtpPass: x "),
    # The forms added in fix round 8.
    "upper_pass_names": _adversarial("ADMIN_PASS=x "),
    "upper_pass_name_runs": _adversarial("A_") + "PASS=x",
    "title_hyphen_pass_names": _adversarial("Root-Pass: x "),
    "title_hyphen_runs": _adversarial("Ab-") + "Pass: x",
    "pass_url_values": _adversarial("proxy_pass: http://x "),
    "pass_url_value_runs": _adversarial("proxy_pass:http://"),
    "placeholder_value_runs": _adversarial("api_key:{secret"),
    "structured_placeholders": _adversarial("api_key: <your-key> "),
    "placeholder_runs": "api_key: <" + _adversarial("a"),
    "curl_context_flags": _adversarial("-U a:b "),
    "curl_context_flags_after_curl": "curl " + _adversarial("-sU a:b "),
    "curl_words_then_flags": _adversarial("curl " * 100 + "-su a:b "),
    "curl_empty_users": _adversarial("curl -u :a "),
    "curl_quoted_pairs_with_blanks": _adversarial("-u 'a:b c' "),
    "curl_unclosed_quoted_pairs": _adversarial("-u 'a:" + "b " * 150),
    "curl_windows_users": _adversarial("-u CORP\\a:b "),
    "curl_unicode_users": _adversarial("-u josé:b "),
    "curl_trailing_punctuation": _adversarial("-u a:b; "),
    "curl_maven_goals": _adversarial("mvn -U a:b "),
    # The forms added in fix round 9.
    "quoted_bearer_words": _adversarial('Bearer "'),
    "quoted_bearer_tokens": _adversarial("Bearer 'Ab1cdefgh' "),
    "bracketed_placeholder_runs": _adversarial("api_key: <your api key "),
    "bracketed_placeholder_opens": _adversarial("api_key: [a "),
    "env_placeholder_runs": _adversarial("DB_PASS=${A"),
    "env_placeholder_values": _adversarial("DB_PASS=$DB_PASS "),
    "percent_placeholder_runs": _adversarial("API_KEY=%A"),
    "caps_placeholder_values": _adversarial("api_key: YOUR_API_KEY_HERE "),
    "helm_filter_runs": _adversarial("api_key: {{ .Values.a | quote "),
    "default_placeholder_runs": _adversarial("DB_PASS=${A:-your_"),
    "bearer_colon_openers": _adversarial("Bearer: [Ab1cdefgh "),
    "caps_env_name_runs": _adversarial("auth_token: AB_CD_EF_GH_"),
    # The forms added in P4 round 3.
    "bearer_next_line_values": _adversarial("bearer:\n  Ab1cdefgh "),
    "bearer_next_line_keys": _adversarial("bearer:\n  Ab1cdefgh" + " " * 20 + ":"),
    # The forms added in P4 round 4: Unicode blanks in the Bearer gap and before a key's ':'.
    "bearer_unicode_gaps": _adversarial("Bearer　"),
    "bearer_unicode_next_lines": _adversarial("bearer: \n "),
    "bearer_unicode_key_blanks": _adversarial("bearer:\n Ab1cdefgh" + " " * 20 + ":"),
}


@pytest.mark.parametrize("name", sorted(_ADVERSARIAL))
def test_scrub_is_linear_on_adversarial_input(name):
    # Later phases scrub whole article bodies with it; a quadratic pattern would hold the GIL for seconds
    # (44 s for the JWT headers before the fix). Measured: under 0.35 s each, so 2 s is a generous bound.
    started = time.perf_counter()
    kc._scrub(_ADVERSARIAL[name])
    assert time.perf_counter() - started < 2.0, name


# Pieces a page may put next to each other: every corpus text, every name form, and the characters
# the rules stop at, split on, or treat as quotes and escapes.
_EDGES = ("", " ", "\t", "\n", ":", "=", ": ", "= ", "&", ";", ",", "}", "]", "[", "{", '"', "'", "\\", "\\\"",
          "“", "”", "‘", "’", "@", "?", "#", "%", "-u", "--user=", "-u ", ".", "/", "<br>", "-sku ", "-U",
          "--proxy-user=", "$(", "`", "Pass", "_pass", "~", "\\\\", "<", ">", "{", "*", "-Pass", "curl ", "http://")
_KEYS = ("api_key: ", "API_TOKEN=", "X-Auth-Token: ", '{"apiToken":', "auth_token:", "x-api-key: ", "db.password=",
         '{\\"password\\":', '\\"api_key\\": ', "“api_key\": ", '{“password": ', "DB_PASS=", "curl -u ",
         "--user=", "?token=", "https://user:", "curl -vu ", "--proxy-user ", "smtpPass: ", "MYSQL_ROOT_PASS=",
         "ADMIN_PASS=", "Root-Pass: ", "proxy_pass: ", "curl -u :", "curl -U ", "mvn -U ", "-u 'CORP\\", "-u ")
_WORDS = ("Bearer", "bearer", "Basic", "Token", "token", "password", "db-pass", "user:pass", "[redacted]",
          "eyJabcdef", "dXNlcjpwYXNz", "Sv1x9Qk2", '"Sv1x9Qk2"', '\\"Sv1x9Qk2\\"', "next", "@h",
          "<password>", "{password}", "changeme", "xxxx", "Pa ss", "josé", "spring-boot:run", ";", ", x",
          "YOUR_API_KEY", "${DB_PASS}", "${", "$DB_PASS", "%API_KEY%", "<your api key>", "[your key]", 'Bearer "',
          "Bearer '")


def _fragment_pools():
    forms = [template.replace("{{", "{").replace("}}", "}") for _, template in _SECRET_FORMS]
    return forms + [text for _, text in _JWT_FORMS], list(_EDGES), list(_KEYS), list(_WORDS)


def test_scrub_never_raises_on_mixed_fragments():
    # Deterministic: the same seeded mixes every run. A scheme word right before the end of the text,
    # a ',', a ';' or a '}' once raised AttributeError, which failed the whole search.
    rng = random.Random(20261105)
    pools = _fragment_pools()
    for _ in range(4000):
        text = "".join(rng.choice(rng.choice(pools)) for _ in range(rng.randint(1, 6)))
        result = kc._scrub(text)
        assert isinstance(result, str), repr(text)


def test_client_module_assigns_each_name_once():
    # A second module-level assignment silently replaces the first one for every earlier function.
    tree = ast.parse(Path(kc.__file__).read_text(encoding="utf-8"))
    names = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names += [n.id for target in node.targets for n in ast.walk(target) if isinstance(n, ast.Name)]
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
    repeated = sorted({name for name in names if names.count(name) > 1})
    assert repeated == [], repeated


_HTML_ADVERSARIAL = {
    "open_brackets": _adversarial("<"),
    "break_tag_spaces": "<br" + _adversarial(" ") + "x",
    "unclosed_tags": _adversarial("<b"),
    "empty_inline_tags": _adversarial("<i></i>"),
    "element_boundaries": _adversarial("</a><a>"),
    "inline_tags_in_words": _adversarial("ab<wbr>"),
    "entities": _adversarial("&amp;"),
    "comparisons": _adversarial("a <= b "),
    "legacy_entities": _adversarial("&sect"),
    "long_entity_names": _adversarial("&" + "a" * 40),
    "tags_after_separators": _adversarial(":<b>a"),
    "tags_before_ats": _adversarial("a</b>@"),
    "tag_runs_after_equals": "=" + _adversarial("<i></i>") + "x",
}


@pytest.mark.parametrize("name", sorted(_HTML_ADVERSARIAL))
def test_search_title_and_description_are_linear_at_full_length(kms, name):
    # Titles and descriptions are scrubbed whole (a cut before the scrub could split a secret), so the
    # tag removal must be linear too. Measured: under 0.3 s each through the mock, so 3 s is a generous bound.
    raw_id = str(uuid.uuid4().int)[:9]
    text = _HTML_ADVERSARIAL[name]
    kms["mock"].STATE["extra_results"] = [_record(raw_id, "Guide " + text, description="Guide " + text)]
    started = time.perf_counter()
    entry = _by_id(client(kms).search_files("x"))[f"item:{raw_id}"]
    assert time.perf_counter() - started < 3.0, name
    assert entry["name"].startswith("Guide") and entry["snippet"].startswith("Guide")


_AI_MARKUP_ADVERSARIAL = {
    "closing_tag_blanks": _adversarial("</" + " " * 40),
    "closing_tag_names": _adversarial("</" + "a" * 60),
    "closing_tag_names_blanks": _adversarial("</ab" + " " * 40),
    "break_blanks": _adversarial("<br" + " " * 40 + "/"),
    "attribute_runs": "<span" + _adversarial(" a=b") + "x",
    "attribute_name_runs": "<span " + _adversarial("a"),
    "attribute_quote_runs": "<span a=\"" + _adversarial("b"),
    "attribute_blank_runs": "<a" + _adversarial(" a = b "),
    "bare_known_tags": _adversarial("<code> "),
    "comment_openers": _adversarial("<!-"),
    "open_brackets": _adversarial("<"),
    "open_brackets_blanks": _adversarial("< "),
    "ampersands": _adversarial("&"),
    "entity_digits": _adversarial("&#" + "1" * 12),
    "entity_names": _adversarial("&" + "a" * 40),
    "plain_words": _adversarial("plain words "),
}


@pytest.mark.parametrize("name", sorted(_AI_MARKUP_ADVERSARIAL))
def test_ai_element_check_is_linear_at_full_length(name):
    # Every ai element is checked (tag-shaped '<' or character reference) and rendered from its ai
    # text or its item HTML, at full length. Measured: well under 0.5 s each, so 3 s is a generous bound.
    text = _AI_MARKUP_ADVERSARIAL[name]
    started = time.perf_counter()
    kc._ai_sections([{"element_name": "E", "element_caption": "C", "tab_caption": "T", "element_text": text}],
                    {"E": {"value": text, "type": "EDITOR_FIELD"}})
    assert time.perf_counter() - started < 3.0, name


def test_search_scrubs_a_title_or_description_at_full_length(kms):
    filler = "<i></i>" * 700                 # 4900 characters that strip to nothing
    secret = "Sv" + uuid.uuid4().hex[:8] + "9Q"
    entry = _search_one(kms, filler + f" Tail words {_URL}?token={secret}")
    for field in ("name", "snippet"):
        assert "Guide" in entry[field] and "Tail words" in entry[field], (field, entry[field])
        assert secret not in entry[field] and "<" not in entry[field], (field, entry[field])


_STRADDLE_FORMS = [
    ("url_userinfo", "https://user:{s}@kms.example.com/x"),
    ("authorization_basic", "Authorization: Basic {s}"),
    ("bearer", "Authorization: Bearer {s}"),
    ("structured_client_secret", "client_secret: {s}"),
    ("query_token", _URL + "?token={s}&lang=en"),
    ("json_password", '{{"password": "{s}"}}'),
    ("jwt", "{s}"),
]


@pytest.mark.parametrize("form,template", _STRADDLE_FORMS, ids=[f for f, _ in _STRADDLE_FORMS])
@pytest.mark.parametrize("offset", [6, 8, 10])
def test_search_secret_across_the_old_4_kb_cut_leaves_nothing(kms, form, template, offset):
    secret = (_H + "." + _P + "." + _S) if form == "jwt" else "Sv" + uuid.uuid4().hex[:8] + "9Q"
    text = template.format(s=secret)
    at = text.index(secret) + offset         # this character of the secret sits at raw offset 4096
    pad_len = 4096 - at - len("Guide ")
    pad = "<i></i>" * (pad_len // 7) + " " * (pad_len % 7)
    raw = "Guide " + pad + text
    assert raw.index(secret) + offset == 4096
    raw_id = str(uuid.uuid4().int)[:9]
    kms["mock"].STATE["extra_results"] = [_record(raw_id, raw, description=raw + " end")]
    entry = _by_id(client(kms).search_files("x"))[f"item:{raw_id}"]
    for field in ("name", "snippet"):
        for piece in (secret, secret[:offset], secret[offset:]):
            assert piece not in entry[field], (form, offset, field, entry[field])
    assert "Guide" in entry["snippet"]


def test_search_snippet_has_no_control_or_bidi_characters(kms):
    raw_id = str(uuid.uuid4().int)[:9]
    description = ("Step\x1b[31m one‮ two\x00 three⁦ four⁩ five‪ six\nseven\x1feight"
                   "\tnine \ud800 ten")
    kms["mock"].STATE["extra_results"] = [_record(raw_id, description=description)]
    snippet = _by_id(client(kms).search_files("x"))[f"item:{raw_id}"]["snippet"]
    assert not _has_control(snippet), repr(snippet)
    for word in ("Step", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"):
        assert word in snippet, (word, snippet)


# ------------------------------------------------------------------ ids
def test_search_skips_records_whose_id_is_not_only_digits(kms, caplog):
    caplog.set_level(logging.DEBUG, logger=kc.__name__)
    base = 70000 + uuid.uuid4().int % 10000
    bad = [f"{base}\n", f" {base + 1}", f"x-{base + 2}", f"{base + 3}.0"]
    kms["mock"].STATE["extra_results"] = [_record(raw) for raw in bad] + [_record(base + 4)]
    entries = client(kms).search_files("x")
    # The good records (alice's visible Online articles and scenario, plus the new one); no bad one.
    assert {e["id"] for e in entries} == {"item:279", "item:301", "scenario:4400", "item:4420", "item:4430",
                                         f"item:{base + 4}"}
    logs = [r.getMessage() for r in caplog.records if r.name == kc.__name__ and "skipped" in r.getMessage()]
    assert logs and str(len(bad)) in logs[-1], logs
    assert all(f"x-{base + 2}" not in m for m in logs)


def test_search_answer_with_only_bad_ids_fails_closed(kms, caplog):
    caplog.set_level(logging.DEBUG, logger=kc.__name__)
    marker = "bad-id-marker-" + uuid.uuid4().hex
    bad = [_record(f"{marker}-{i}", marker) for i in range(7)]
    kms["mock"].STATE["search_body"] = {"regularSearch": {"query": "x", "results": bad}}
    with pytest.raises(kc.KmsHTTPError) as info:
        token_client(kms).search_files("billing")
    assert info.value.kind == "server" and marker not in str(info.value)
    assert info.value.__context__ is None and info.value.__cause__ is None
    warnings = [r.getMessage() for r in caplog.records if r.name == kc.__name__ and r.levelno == logging.WARNING]
    assert warnings and re.findall(r"\d+", warnings[-1]) == ["7"], warnings
    assert all(marker not in r.getMessage() for r in caplog.records)
    # Bad ids next to a record that is merely hidden (offline) are a real "nothing to show".
    kms["mock"].STATE["search_body"] = {"regularSearch": {"query": "x", "results": bad + [
        _record(81234, status="Offline")]}}
    assert token_client(kms).search_files("billing") == []


def test_search_with_only_bad_ids_on_every_page_fails_closed(kms, caplog):
    # Twenty full pages of unusable records are an unexpected answer, not "more results may exist".
    caplog.set_level(logging.DEBUG, logger=kc.__name__)
    bad = [_record(f"x-{i}") for i in range(kc._SEARCH_PAGE_ROWS)]
    kms["mock"].STATE["search_body"] = {"regularSearch": {"query": "x", "results": bad}}
    with pytest.raises(kc.KmsHTTPError) as info:
        token_client(kms).search_files("billing", limit=10_000)
    assert not isinstance(info.value, SearchStopped) and info.value.kind == "server"
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert kms["mock"].STATE["search_count"] == kc._MAX_SEARCH_PAGES
    warnings = [r.getMessage() for r in caplog.records if r.name == kc.__name__ and r.levelno == logging.WARNING]
    assert warnings and re.findall(r"\d+", warnings[-1]) == [str(kc._SEARCH_PAGE_ROWS * kc._MAX_SEARCH_PAGES)]


def test_search_timeout_wrapped_by_requests_at_the_deadline_is_a_stop(kms, monkeypatch):
    # requests wraps urllib3's read timeout while streaming in a ConnectionError.
    original = requests.Session.request
    wrapped = {"timeout": True}

    def slow(self, method, url, *args, **kwargs):
        if url.endswith("/search/full"):
            inner = urllib3.exceptions.ReadTimeoutError(None, None, "Read timed out.") if wrapped["timeout"] else None
            raise requests.ConnectionError(inner) if inner else requests.ConnectionError("refused")
        return original(self, method, url, *args, **kwargs)

    monkeypatch.setattr(requests.Session, "request", slow)
    with pytest.raises(SearchStopped) as info:
        token_client(kms).search_files("billing", deadline=time.monotonic() + 0.5)
    assert info.value.stop_reason == "deadline" and info.value.files == []
    # The same wrapper without a timeout inside is "unreachable", deadline or not.
    wrapped["timeout"] = False
    with pytest.raises(kc.KmsHTTPError) as info:
        token_client(kms).search_files("billing", deadline=time.monotonic() + 0.5)
    assert info.value.kind == "unreachable" and not isinstance(info.value, SearchStopped)


def test_tree_record_id_must_be_only_digits(kms, monkeypatch):
    c = token_client(kms)
    shape = {"status": "SUCCESS", "data": [{"id": "90\n", "title": "Folder", "type": "FOLDER", "status": "2"}]}
    monkeypatch.setattr(c, "_request", lambda op, path, **kw: shape)
    result = c.test_connection()
    assert result["success"] is False and result["message"] == str(kc.KmsHTTPError(0, "server"))


# ------------------------------------------------------------------ odd search bodies
@pytest.mark.parametrize("block", [{"query": "x"}, {"query": "x", "results": None}])
def test_search_without_results_is_a_server_error(kms, caplog, block):
    caplog.set_level(logging.DEBUG, logger=kc.__name__)
    kms["mock"].STATE["search_body"] = {"regularSearch": block}
    with pytest.raises(kc.KmsHTTPError) as info:
        token_client(kms).search_files("billing")
    assert info.value.kind == "server"
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert any(r.name == kc.__name__ and "search refused" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("block", [{"query": "x", "total": 0}, {"query": "x", "total": "0"},
                                   {"query": "x", "results": []}])
def test_search_with_an_explicit_empty_answer_has_no_hits(kms, block):
    kms["mock"].STATE["search_body"] = {"regularSearch": block}
    assert token_client(kms).search_files("billing") == []


# ------------------------------------------------------------------ limit and timeout
@pytest.mark.parametrize("limit", ["abc", "", [3], object()])
def test_bad_limit_is_named_not_blamed_on_kms(kms, limit):
    with pytest.raises(kc.KmsError) as info:
        token_client(kms).search_files("billing", limit=limit)
    err = info.value
    assert "limit" in str(err).lower() and "KMS Lighthouse returned" not in str(err)
    assert err.__context__ is None and err.__cause__ is None
    assert kms["mock"].REQUEST_LOG == []


def test_numeric_text_limit_is_accepted(kms):
    assert len(token_client(kms).search_files("billing", limit="2")) == 2


def test_search_read_timeout_follows_the_deadline(kms, monkeypatch):
    seen = []
    original = requests.Session.request

    def spy(self, method, url, *args, **kwargs):
        if url.endswith("/search/full"):
            seen.append(kwargs.get("timeout"))
        return original(self, method, url, *args, **kwargs)

    monkeypatch.setattr(requests.Session, "request", spy)
    c = token_client(kms)
    c.search_files("billing")
    assert seen[-1][1] == 60                          # no deadline: the default read timeout
    c.search_files("billing", deadline=time.monotonic() + 30)
    connect, read = seen[-1]
    assert 1 <= read <= 30 and connect <= read
    c.search_files("billing", deadline=time.monotonic() + 5)
    assert 1 <= seen[-1][1] <= 5                      # cut to the time left
    monkeypatch.setattr(kc, "_MIN_TIMEOUT_S", 8)
    c.search_files("billing", deadline=time.monotonic() + 5)
    assert seen[-1] == (8, 8)                         # never below the floor

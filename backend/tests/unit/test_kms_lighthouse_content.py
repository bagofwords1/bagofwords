"""KMS Lighthouse content contract (search; browse and read arrive later) against the mock KMS API.

Same boundary as test_kms_lighthouse_client.py: a real HTTP server on a real
socket (tools/kms_lighthouse/mock_kms_server.py), everything on our side real.
Behaviour is asserted on the public methods' output only.
"""
# The shared fixtures are imported from the client test module, so test
# parameters named after them look like redefinitions to the linter.
# ruff: noqa: F811
import logging
import re
import time
import unicodedata
import uuid
from datetime import datetime
from urllib.parse import quote

import pytest
import requests
import urllib3

from app.ai.tools.implementations._file_tool_common import _AUTH_ERROR_MARKERS
from app.data_sources.clients import kms_lighthouse_client as kc
from app.data_sources.clients.base import SearchStopped
from tests.unit.test_kms_lighthouse_client import client, kms, kms_server, token_client  # noqa: F401

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


# ------------------------------------------------------------------ closed errors
_METHODS = {
    "search": lambda c: c.search_files("billing"),
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


# Every public method that exists so far (P4/P5 extend this list).
_PUBLIC_METHODS = ("test_connection", "search_files")


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
                          "aws_secret_access_key", "user-passwd", "x-api-key", "AccessKey", "php_sessionid")


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
    ("yaml_single_quoted", "'secret': '{s}'"),
    ("structured_session_id", "session_id: {s}"),
    # A raw '@' inside a URL password, and a scheme right after a digit, '.' or '-'.
    ("userinfo_at_inside_password", "https://user:{s1}@{s2}@kms.example.com/x"),
    ("userinfo_after_digit", "7https://user:{s}@kms.example.com/x"),
    ("userinfo_after_dot", "1.https://user:{s}@kms.example.com/x"),
    ("userinfo_after_dash", "--https://user:{s}@kms.example.com/x"),
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
                                  "Basic Authentication setup"])
def test_search_scrub_keeps_ordinary_prose(kms, text):
    entry = _search_one(kms, text)
    assert text in entry["snippet"], entry["snippet"]
    assert text in entry["name"], entry["name"]


@pytest.mark.parametrize("text", ["x <= 5 and y >= 3", "Orders < 100 and > 50 NIS", "5 < 6 > 4",
                                  "Discount: <50% off> today", "?page=1&section=2&times=3",
                                  "Q&A &notifications", "R&D &copy2026 &notin"])
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
    "prose_names": _adversarial("token "),
    "userinfo": "https://" + _adversarial("a:"),
    "joiners": _adversarial("a\u200d"),
    "entities": _adversarial("&amp;#61;"),
    "named_entities": _adversarial("&amp;equals;"),
    "amp_prefixes": "?" + _adversarial("amp;") + "token=x",
    "spaced_empty_values": _adversarial("?q= "),
    "quoted_secret_values": _adversarial("?token='"),
    "unclosed_quotes": _adversarial("?token='" + "a" * 300),
    "prose_quotes": _adversarial('password: "'),
    "json_names": _adversarial('"token":'),
    "prose_values": _adversarial("token: Ab1 "),
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
    "bearer_unspaced": _adversarial("Bearer"),
    "basic_letters": _adversarial("Basic dXNlcjpwYXNz "),
}


@pytest.mark.parametrize("name", sorted(_ADVERSARIAL))
def test_scrub_is_linear_on_adversarial_input(name):
    # Later phases scrub whole article bodies with it; a quadratic pattern would hold the GIL for seconds
    # (44 s for the JWT headers before the fix). Measured: under 0.35 s each, so 2 s is a generous bound.
    started = time.perf_counter()
    kc._scrub(_ADVERSARIAL[name])
    assert time.perf_counter() - started < 2.0, name


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

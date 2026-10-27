"""Teams 1:1 progress bubble (informative streaming).

Drives the real TeamsAdapter + teams_stream against a fake Bot Connector
(httpx.MockTransport — the Teams HTTP API is the boundary) and asserts what
Teams receives: one bubble per working stretch whose line updates in place,
turned into the next outbound message, closed when the run ends, and never
at the cost of losing a reply.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio

from app.core import fire_and_forget
from app.services.platform_adapters import teams_adapter as teams_adapter_mod
from app.services.platform_adapters import teams_stream
from app.services.platform_adapters.teams_adapter import TeamsAdapter
from app.services.teams_status_strings import STRINGS, status_locale, tool_status_text


class FakeTeams:
    """Minimal Bot Connector: hands out stream ids and records activities."""

    def __init__(self):
        self.activities = []
        self._next_stream = 0
        # stream id -> (status, body) returned for any later request on it
        self.stream_errors = {}
        self.refuse_streams = False
        # When set, Teams holds every reply until the event fires.
        self.gate = None
        # Requests that fail on the network before reaching Teams.
        self.network_failures = 0
        # Requests Teams answers with 503 (busy).
        self.busy_responses = 0
        # What happens to the next reply finals: a status code, or an
        # httpx exception class raised instead of an answer.
        self.final_failures = []

    async def async_handler(self, request: httpx.Request) -> httpx.Response:
        if self.gate is not None and "oauth2" not in request.url.path:
            await self.gate.wait()
        if self.network_failures and "oauth2" not in request.url.path:
            self.network_failures -= 1
            raise httpx.ConnectError("network down")
        if self.final_failures and "oauth2" not in request.url.path:
            activity = json.loads(request.content)
            if _is_reply_final(activity):
                failure = self.final_failures.pop(0)
                self.activities.append(activity)
                if isinstance(failure, type):
                    raise failure("teams trouble")
                return httpx.Response(failure, json={"error": {"code": "BadArgument"}})
        if self.busy_responses and "oauth2" not in request.url.path:
            self.busy_responses -= 1
            return httpx.Response(503, json={"error": {"code": "ServiceUnavailable"}})
        return self.handler(request)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if "oauth2" in request.url.path:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        activity = json.loads(request.content)
        self.activities.append(activity)
        info = _stream_info(activity)
        if info is None:
            return httpx.Response(201, json={"id": f"m{len(self.activities)}"})
        if "streamId" not in info:
            if self.refuse_streams:
                return httpx.Response(403, json={"error": {"code": "ContentStreamNotAllowed",
                                                           "message": "Content stream is not allowed"}})
            self._next_stream += 1
            return httpx.Response(201, json={"id": f"s{self._next_stream}"})
        if info["streamId"] in self.stream_errors:
            status, message = self.stream_errors[info["streamId"]]
            return httpx.Response(status, json={"error": {"code": "ContentStreamNotAllowed", "message": message}})
        return httpx.Response(202, json={})

    # ── views ──
    def finals(self):
        return [a for a in self.activities if (_stream_info(a) or {}).get("streamType") == "final"]

    def plain_messages(self):
        return [a for a in self.activities if a["type"] == "message" and _stream_info(a) is None]

    def opens(self):
        return [a for a in self.activities
                if a["type"] == "typing" and "streamId" not in (_stream_info(a) or {"streamId": 1})]


def _is_reply_final(activity):
    """A stream final carrying a reply (not one of our own status lines)."""
    info = _stream_info(activity) or {}
    status_lines = {line for strings in STRINGS.values() for line in strings.values()}
    return info.get("streamType") == "final" and activity.get("text") not in status_lines


def _stream_info(activity):
    for e in activity.get("entities") or []:
        if e.get("type") == "streaminfo":
            return e
    return None


@pytest_asyncio.fixture
async def teams(monkeypatch):
    fake = FakeTeams()
    original = httpx.AsyncClient

    class _Client(original):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(fake.async_handler)
            super().__init__(*a, **kw)

    monkeypatch.setattr(teams_adapter_mod.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(TeamsAdapter, "_token_cache", {"token": "", "expires_at": 0})
    monkeypatch.setattr(teams_stream, "MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(teams_stream, "END_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(teams_stream, "_conversations", {})
    yield fake
    timers = [c.expiry for c in teams_stream._conversations.values() if c.expiry is not None]
    for t in timers:
        t.cancel()
    await asyncio.gather(*timers, return_exceptions=True)


def _adapter():
    platform = SimpleNamespace(
        platform_config={"tenant_id": "t1", "service_url": "https://smba.example/teams/", "bot_id": "28:bot"},
        decrypt_credentials=lambda: {"app_id": "app", "client_secret": "secret"},
    )
    return TeamsAdapter(platform)


async def _settle():
    """Wait for spawned work, except the long two-minute expiry timers."""
    loop = asyncio.get_running_loop()
    for _ in range(10):
        # Re-read each round: a bubble opened meanwhile starts a new timer.
        timers = {c.expiry for c in teams_stream._conversations.values()}
        pending = [t for t in list(fire_and_forget._pending_tasks)
                   if t not in timers and t.get_coro().__name__ != "_expire"
                   and not t.done() and t.get_loop() is loop]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


CONV = "a:conv-1"


# ── status lines ────────────────────────────────────────────────────────


@pytest.mark.parametrize("question,org_locale,expected", [
    ("כמה מכירות היו?", "en", "he"),
    ("كم عدد المبيعات؟", None, "ar"),
    ("Сколько продаж?", "he", "ru"),
    ("How many sales?", "es", "es"),        # Latin question, Latin org locale
    ("How many sales?", "he", "en"),        # Latin question, non-Latin org locale
    ("How many sales?", None, "en"),
    ("12345", "he", "he"),                  # nothing to detect: org locale
    ("", None, "en"),
])
def test_status_language_follows_the_question(question, org_locale, expected):
    assert status_locale(question, org_locale) == expected


@pytest.mark.parametrize("tool", ["create_data", "run_query", "create_artifact", "create_doc", "create_instruction"])
def test_tool_lines_name_what_the_tool_works_on(tool):
    line = tool_status_text("he", tool, "לקוחות לפי מדינה")
    assert "לקוחות לפי מדינה" in line
    assert line != tool_status_text("he", tool)


def test_long_titles_are_cut_and_tidied():
    title = "  מכירות   לפי\nמדינה " + "ומוצר " * 40
    line = tool_status_text("en", "create_data", title)
    shown = line.removeprefix("Running a query: ").removesuffix("…")
    assert len(shown) <= 60
    assert "\n" not in line and "  " not in line
    assert shown.startswith("מכירות לפי מדינה")


def test_a_title_on_a_tool_without_a_titled_line_is_ignored():
    assert tool_status_text("en", "describe_tables", "orders") == STRINGS["en"]["inspect"]
    assert tool_status_text("en", "some_new_tool", "x") == STRINGS["en"]["working"]


@pytest.mark.parametrize("tool", ["create_instruction", "edit_instruction"])
def test_instruction_tools_say_they_update_instructions(tool):
    assert tool_status_text("he", tool) == "מעדכן הנחיות…"


def test_every_locale_has_every_status_line():
    keys = set(STRINGS["en"])
    for locale, strings in STRINGS.items():
        assert set(strings) == keys, locale
        assert all(v.strip() for v in strings.values()), locale


# ── bubble lifecycle ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_question_opens_bubble_and_tool_lines_update_it_in_place(teams):
    adapter = _adapter()
    await teams_stream.start_run(adapter, CONV, "כמה מכירות היו?")
    await teams_stream.notify_tool_started(CONV, "run-1", "create_data")
    await teams_stream.notify_tool_started(CONV, "run-1", "create_artifact")

    typing = [a for a in teams.activities if a["type"] == "typing"]
    assert [a["text"] for a in typing] == [STRINGS["he"]["working"], STRINGS["he"]["query"], STRINGS["he"]["dashboard"]]
    infos = [_stream_info(a) for a in typing]
    assert all(i["streamType"] == "informative" for i in infos)
    # One bubble: later updates carry the id Teams returned for the first one,
    # with a sequence that starts at 1 and grows by one.
    assert "streamId" not in infos[0]
    assert {i["streamId"] for i in infos[1:]} == {"s1"}
    assert [i["streamSequence"] for i in infos] == [1, 2, 3]
    assert all(a["conversation"]["id"] == CONV for a in typing)


@pytest.mark.asyncio
async def test_status_lines_fall_back_to_the_org_language(teams):
    await teams_stream.start_run(_adapter(), CONV, "How many sales?", "es")

    assert [a["text"] for a in teams.opens()] == [STRINGS["es"]["working"]]


@pytest.mark.asyncio
async def test_a_reply_that_beats_the_first_bubble_is_not_followed_by_one(teams):
    adapter = _adapter()
    opening = teams_stream.start_run(adapter, CONV, "How many sales?")
    assert await adapter.send_dm_in_thread("user-1", "Sales grew 12%.", CONV, channel_id=CONV)
    await opening

    assert teams.opens() == []
    assert [m["text"] for m in teams.plain_messages()] == ["Sales grew 12%."]


@pytest.mark.asyncio
async def test_reply_takes_over_the_bubble_and_next_tool_opens_a_new_one(teams):
    adapter = _adapter()
    await teams_stream.start_run(adapter, CONV, "How many sales?")
    await teams_stream.notify_tool_started(CONV, "run-1", "create_data")

    assert await adapter.send_dm_in_thread("user-1", "Sales grew 12%.", CONV, channel_id=CONV)

    finals = teams.finals()
    assert len(finals) == 1
    assert finals[0]["type"] == "message"
    assert finals[0]["text"] == "Sales grew 12%."
    assert _stream_info(finals[0])["streamId"] == "s1"
    assert "streamSequence" not in _stream_info(finals[0])
    assert teams.plain_messages() == []          # not sent twice

    # The agent keeps working: a fresh bubble, and later messages close it too.
    await teams_stream.notify_tool_started(CONV, "run-1", "create_data")
    assert len(teams.opens()) == 2
    assert await adapter.send_dm_in_thread("user-1", "Here is the breakdown.", CONV, channel_id=CONV)
    assert [_stream_info(f)["streamId"] for f in teams.finals()] == ["s1", "s2"]


@pytest.mark.asyncio
async def test_chart_file_takes_over_the_bubble_with_its_attachment(teams, tmp_path):
    adapter = _adapter()
    chart = tmp_path / "chart.png"
    chart.write_bytes(b"\x89PNG fake")
    await teams_stream.start_run(adapter, CONV, "chart please")

    assert await adapter.send_file_in_thread("user-1", str(chart), "Sales by month", CONV, channel_id=CONV)

    finals = teams.finals()
    assert len(finals) == 1
    assert finals[0]["text"] == "Sales by month"
    assert finals[0]["attachments"][0]["name"] == "chart.png"


@pytest.mark.asyncio
async def test_reply_is_still_delivered_when_teams_already_closed_the_bubble(teams):
    adapter = _adapter()
    await teams_stream.start_run(adapter, CONV, "How many sales?")
    teams.stream_errors["s1"] = (403, "Content stream finished due to exceeded streaming time.")

    assert await adapter.send_dm_in_thread("user-1", "Sales grew 12%.", CONV, channel_id=CONV)

    assert [m["text"] for m in teams.plain_messages()] == ["Sales grew 12%."]


@pytest.mark.asyncio
async def test_long_step_hands_over_to_a_fresh_bubble_before_teams_two_minute_limit(teams, monkeypatch):
    monkeypatch.setattr(teams_stream, "STREAM_MAX_SECONDS", 0.0)
    adapter = _adapter()
    await teams_stream.start_run(adapter, CONV, "How many sales?")
    first_timer = teams_stream._conversations[CONV].expiry
    monkeypatch.setattr(teams_stream, "STREAM_MAX_SECONDS", 1000.0)
    await first_timer

    # The old bubble keeps the step it showed; a new one carries on live.
    assert [f["text"] for f in teams.finals()] == [STRINGS["en"]["working"]]
    assert _stream_info(teams.finals()[0])["streamId"] == "s1"
    assert [o["text"] for o in teams.opens()] == [STRINGS["en"]["working"]] * 2
    await teams_stream.notify_tool_started(CONV, "run-1", "create_data")
    assert _stream_info(teams.activities[-1])["streamId"] == "s2"


@pytest.mark.asyncio
async def test_no_fresh_bubble_after_the_run_ended(teams, monkeypatch):
    monkeypatch.setattr(teams_stream, "STREAM_MAX_SECONDS", 0.0)
    await teams_stream.start_run(_adapter(), CONV, "How many sales?")
    conv = teams_stream._conversations[CONV]
    first_timer = conv.expiry
    conv.open_runs = 0                       # the run finished meanwhile
    await first_timer

    assert len(teams.opens()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network_failures", "busy_responses"])
async def test_a_passing_failure_does_not_switch_the_bubble_off(teams, failure):
    setattr(teams, failure, 1)               # the first request fails, Teams is fine after
    adapter = _adapter()
    await teams_stream.start_run(adapter, CONV, "How many sales?")
    assert teams.opens() == []

    await teams_stream.notify_tool_started(CONV, "run-1", "create_data")

    assert [o["text"] for o in teams.opens()] == [STRINGS["en"]["query"]]


@pytest.mark.asyncio
async def test_bubbles_stop_for_good_after_the_hard_cap(teams, monkeypatch):
    monkeypatch.setattr(teams_stream, "STREAM_MAX_SECONDS", 0.0)
    monkeypatch.setattr(teams_stream, "MAX_RUN_SECONDS", 0.0)
    await teams_stream.start_run(_adapter(), CONV, "How many sales?")
    first_timer = teams_stream._conversations[CONV].expiry
    await first_timer

    # The bubble closes with its line, no new one opens, the chat is dropped.
    assert [f["text"] for f in teams.finals()] == [STRINGS["en"]["working"]]
    assert len(teams.opens()) == 1
    assert CONV not in teams_stream._conversations
    assert teams_stream.notify_tool_started(CONV, "run-1", "create_data") is None


@pytest.mark.asyncio
async def test_a_new_question_gets_its_own_bubble_below_it(teams):
    adapter = _adapter()
    await teams_stream.start_run(adapter, CONV, "first?")
    await teams_stream.notify_tool_started(CONV, "run-1", "create_data")
    await teams_stream.start_run(adapter, CONV, "second?")

    # The first question's bubble keeps its line; the second gets a new one.
    assert [f["text"] for f in teams.finals()] == [STRINGS["en"]["query"]]
    assert [o["text"] for o in teams.opens()] == [STRINGS["en"]["working"]] * 2
    assert "streamId" not in _stream_info(teams.opens()[1])


@pytest.mark.asyncio
async def test_back_to_back_questions_still_get_a_bubble(teams):
    adapter = _adapter()
    first = teams_stream.start_run(adapter, CONV, "first?")
    second = teams_stream.start_run(adapter, CONV, "second?")
    await asyncio.gather(first, second)

    assert len(teams.opens()) == 1
    assert teams_stream._conversations[CONV].stream_id is not None


@pytest.mark.asyncio
async def test_reply_with_unknown_delivery_is_not_sent_twice(teams):
    adapter = _adapter()
    await teams_stream.start_run(adapter, CONV, "How many sales?")
    teams.final_failures = [httpx.ReadTimeout]   # sent, but the answer never came back

    assert not await adapter.send_dm_in_thread("user-1", "Sales grew 12%.", CONV, channel_id=CONV)

    replies = [a for a in teams.activities if a.get("text") == "Sales grew 12%."]
    assert len(replies) == 1


@pytest.mark.asyncio
async def test_reply_that_never_reached_teams_is_sent_again(teams):
    adapter = _adapter()
    await teams_stream.start_run(adapter, CONV, "How many sales?")
    teams.network_failures = 1                   # the final never leaves this server

    assert await adapter.send_dm_in_thread("user-1", "Sales grew 12%.", CONV, channel_id=CONV)

    assert [m["text"] for m in teams.plain_messages()] == ["Sales grew 12%."]


@pytest.mark.asyncio
async def test_rejected_reply_closes_the_bubble_then_goes_out_on_its_own(teams, tmp_path):
    adapter = _adapter()
    chart = tmp_path / "chart.png"
    chart.write_bytes(b"\x89PNG fake")
    await teams_stream.start_run(adapter, CONV, "How many sales?")
    teams.final_failures = [400]                 # Teams refuses the file as a stream final

    assert await adapter.send_file_in_thread("user-1", str(chart), "Sales by month", CONV, channel_id=CONV)

    # The bubble is closed with its own line, and the file goes out plain.
    assert STRINGS["en"]["working"] in [f["text"] for f in teams.finals()]
    assert [m["text"] for m in teams.plain_messages()] == ["Sales by month"]


@pytest.mark.asyncio
async def test_stop_noticed_on_a_reply_stops_the_run(teams):
    adapter = _adapter()
    await teams_stream.start_run(adapter, CONV, "How many sales?")
    await teams_stream.notify_tool_started(CONV, "run-1", "create_data")
    teams.stream_errors["s1"] = (403, "Content stream was canceled by user.")

    with patch(
        "app.services.completion_service.CompletionService.update_completion_sigkill",
        new=AsyncMock(),
    ) as sigkill:
        sent = await adapter.send_dm_in_thread("user-1", "Sales grew 12%.", CONV, channel_id=CONV)
        await _settle()

    assert sent is False
    assert [c.args[1] for c in sigkill.await_args_list] == ["run-1"]
    assert teams.plain_messages() == []          # the stopped reply is discarded
    assert teams_stream.notify_tool_started(CONV, "run-1", "create_data") is None


@pytest.mark.asyncio
async def test_stop_noticed_on_the_handover_stops_the_run(teams, monkeypatch):
    monkeypatch.setattr(teams_stream, "STREAM_MAX_SECONDS", 0.0)
    await teams_stream.start_run(_adapter(), CONV, "How many sales?")
    conv = teams_stream._conversations[CONV]
    conv.run_ids.add("run-1")
    first_timer = conv.expiry
    monkeypatch.setattr(teams_stream, "STREAM_MAX_SECONDS", 1000.0)
    teams.stream_errors["s1"] = (403, "Content stream was canceled by user.")

    with patch(
        "app.services.completion_service.CompletionService.update_completion_sigkill",
        new=AsyncMock(),
    ) as sigkill:
        await first_timer
        await _settle()

    assert [c.args[1] for c in sigkill.await_args_list] == ["run-1"]
    assert len(teams.opens()) == 1               # no fresh bubble after Stop


@pytest.mark.asyncio
async def test_stop_on_the_previous_bubble_still_gives_the_new_question_a_bubble(teams):
    adapter = _adapter()
    await teams_stream.start_run(adapter, CONV, "first?")
    await teams_stream.notify_tool_started(CONV, "run-1", "create_data")
    teams.stream_errors["s1"] = (403, "Content stream was canceled by user.")

    with patch(
        "app.services.completion_service.CompletionService.update_completion_sigkill",
        new=AsyncMock(),
    ) as sigkill:
        await teams_stream.start_run(adapter, CONV, "second?")
        await _settle()

    # The Stop stops the first question's run; the second still gets a bubble.
    assert [c.args[1] for c in sigkill.await_args_list] == ["run-1"]
    assert [o["text"] for o in teams.opens()] == [STRINGS["en"]["working"]] * 2
    assert teams_stream.notify_tool_started(CONV, "run-2", "create_data") is not None


@pytest.mark.asyncio
async def test_stop_leaves_finished_runs_alone(teams):
    adapter = _adapter()
    await teams_stream.start_run(adapter, CONV, "first?")
    await teams_stream.notify_tool_started(CONV, "run-1", "create_data")
    await teams_stream.start_run(adapter, CONV, "second?")
    await teams_stream.end_run(CONV, "run-1")    # the first question is done
    await teams_stream.notify_tool_started(CONV, "run-2", "create_data")
    stream_id = teams_stream._conversations[CONV].stream_id
    teams.stream_errors[stream_id] = (403, "Content stream was canceled by user.")

    with patch(
        "app.services.completion_service.CompletionService.update_completion_sigkill",
        new=AsyncMock(),
    ) as sigkill:
        await teams_stream.notify_tool_started(CONV, "run-2", "describe_tables")
        await _settle()

    assert [c.args[1] for c in sigkill.await_args_list] == ["run-2"]


@pytest.mark.asyncio
async def test_stop_button_stops_the_run_and_the_bubble(teams):
    adapter = _adapter()
    await teams_stream.start_run(adapter, CONV, "How many sales?")
    await teams_stream.notify_tool_started(CONV, "run-1", "create_data")
    teams.stream_errors["s1"] = (403, "Content stream was canceled by user.")

    with patch(
        "app.services.completion_service.CompletionService.update_completion_sigkill",
        new=AsyncMock(),
    ) as sigkill:
        await teams_stream.notify_tool_started(CONV, "run-1", "create_artifact")
        await _settle()

    assert [c.args[1] for c in sigkill.await_args_list] == ["run-1"]
    assert all(c.kwargs.get("authorize") is False for c in sigkill.await_args_list)
    sent = len(teams.activities)
    assert teams_stream.notify_tool_started(CONV, "run-1", "create_data") is None
    assert len(teams.activities) == sent      # no new bubble after Stop


@pytest.mark.asyncio
async def test_run_end_closes_an_unclaimed_bubble_and_forgets_the_chat(teams):
    adapter = _adapter()
    await teams_stream.start_run(adapter, CONV, "How many sales?")

    await teams_stream.end_run(CONV)

    assert [f["text"] for f in teams.finals()] == [STRINGS["en"]["done"]]
    assert CONV not in teams_stream._conversations
    # Late tool events from the finished run do nothing.
    assert teams_stream.notify_tool_started(CONV, "run-1", "create_data") is None


@pytest.mark.asyncio
async def test_failed_run_closes_an_unclaimed_bubble_with_an_error_line(teams):
    adapter = _adapter()
    await teams_stream.start_run(adapter, CONV, "כמה מכירות היו?")
    await teams_stream.notify_tool_started(CONV, "run-1", "create_data")

    await teams_stream.end_run(CONV, "run-1", failed=True)

    assert [f["text"] for f in teams.finals()] == [STRINGS["he"]["failed"]]


@pytest.mark.asyncio
async def test_failed_run_after_its_reply_sends_no_error_line(teams):
    adapter = _adapter()
    await teams_stream.start_run(adapter, CONV, "How many sales?")
    assert await adapter.send_dm_in_thread("user-1", "Sales grew 12%.", CONV, channel_id=CONV)

    await teams_stream.end_run(CONV, "run-1", failed=True)

    assert [f["text"] for f in teams.finals()] == ["Sales grew 12%."]
    assert teams.plain_messages() == []


@pytest.mark.asyncio
async def test_a_new_question_clears_an_earlier_failure(teams):
    adapter = _adapter()
    await teams_stream.start_run(adapter, CONV, "first?")
    await teams_stream.start_run(adapter, CONV, "second?")
    await teams_stream.end_run(CONV, "run-1", failed=True)
    await teams_stream.start_run(adapter, CONV, "third?")

    await teams_stream.end_run(CONV, "run-2")
    await teams_stream.end_run(CONV, "run-3")

    closing_lines = [f["text"] for f in teams.finals()]
    assert closing_lines[-1] == STRINGS["en"]["done"]
    assert STRINGS["en"]["failed"] not in closing_lines


@pytest.mark.asyncio
async def test_a_run_reported_ended_twice_counts_once(teams):
    adapter = _adapter()
    await teams_stream.start_run(adapter, CONV, "first?")
    await teams_stream.start_run(adapter, CONV, "second?")

    # The status listener and the crash handler can both report one run.
    await teams_stream.end_run(CONV, "run-1", failed=True)
    await teams_stream.end_run(CONV, "run-1", failed=True)

    # The second question still owns its bubble: nothing says Done/failed.
    assert STRINGS["en"]["done"] not in [f["text"] for f in teams.finals()]
    assert STRINGS["en"]["failed"] not in [f["text"] for f in teams.finals()]
    assert teams_stream._conversations[CONV].stream_id is not None


@pytest.mark.asyncio
async def test_run_end_keeps_the_bubble_while_another_question_is_running(teams):
    adapter = _adapter()
    await teams_stream.start_run(adapter, CONV, "first?")
    await teams_stream.start_run(adapter, CONV, "second?")

    await teams_stream.end_run(CONV)

    # Only the hand-over from the first question's bubble, no "Done.".
    assert [f["text"] for f in teams.finals()] == [STRINGS["en"]["working"]]
    await teams_stream.notify_tool_started(CONV, "run-2", "create_data")
    assert teams.activities[-1]["text"] == STRINGS["en"]["query"]
    assert _stream_info(teams.activities[-1])["streamId"] == "s2"


@pytest.mark.asyncio
async def test_refused_stream_falls_back_to_plain_messages(teams):
    teams.refuse_streams = True
    adapter = _adapter()
    await teams_stream.start_run(adapter, CONV, "How many sales?")
    attempts = len(teams.activities)
    assert teams_stream.notify_tool_started(CONV, "run-1", "create_data") is None
    assert len(teams.activities) == attempts   # no retry per tool

    assert await adapter.send_dm_in_thread("user-1", "Sales grew 12%.", CONV, channel_id=CONV)
    assert [m["text"] for m in teams.plain_messages()] == ["Sales grew 12%."]


@pytest.mark.asyncio
async def test_chat_without_a_question_bubble_gets_plain_messages(teams):
    adapter = _adapter()
    assert teams_stream.notify_tool_started("19:channel@thread", "run-1", "create_data") is None

    assert await adapter.send_dm_in_thread("user-1", "Hello", "19:channel@thread", channel_id="19:channel@thread")

    assert [_stream_info(a) for a in teams.activities] == [None]


@pytest.mark.asyncio
async def test_bubble_line_names_the_query(teams):
    await teams_stream.start_run(_adapter(), CONV, "כמה לקוחות?")
    await teams_stream.notify_tool_started(CONV, "run-1", "create_data", "לקוחות לפי מדינה")

    assert teams.activities[-1]["text"] == "מריץ שאילתה: לקוחות לפי מדינה…"


@pytest.mark.asyncio
async def test_burst_of_tools_shows_the_latest_line(teams):
    adapter = _adapter()
    await teams_stream.start_run(adapter, CONV, "How many sales?")
    tasks = [
        teams_stream.notify_tool_started(CONV, "run-1", name)
        for name in ("create_data", "describe_tables", "create_doc")
    ]
    await asyncio.gather(*tasks)

    assert teams.activities[-1]["text"] == STRINGS["en"]["doc"]


# ── where the bubble opens ──────────────────────────────────────────────


def _manager_with_report():
    from app.services.external_platform_manager import ExternalPlatformManager

    m = ExternalPlatformManager()
    m.mapping_service.get_user_by_id = AsyncMock(return_value=SimpleNamespace(id="u1", name="Alice"))
    m.organization_service.get_organization = AsyncMock(return_value=SimpleNamespace(id="org1"))
    m.completion_service.create_completion = AsyncMock()
    m.data_source_service.get_active_data_sources = AsyncMock(return_value=[])
    m.data_source_service.get_public_data_sources = AsyncMock(return_value=[])
    return m


def _db():
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_type,conversation,bubble", [
    ("personal", "a:conv-1", True),
    ("channel", "19:abc@thread.tacv2", False),
])
async def test_only_personal_chat_questions_open_a_bubble(teams, channel_type, conversation, bubble):
    m = _manager_with_report()
    report = SimpleNamespace(id="R1", title="Chat")
    data = {
        "platform_type": "teams", "message_text": "How many sales?",
        "thread_ts": conversation, "message_ts": "msg-1", "channel_id": conversation,
        "channel_type": channel_type, "is_thread_reply": True,
    }
    mapping = SimpleNamespace(app_user_id="u1", organization_id="org1", external_user_id="ext-1",
                              platform_type="teams", platform_id="plat-1")
    with patch("app.core.permission_resolver.principal_belongs_to_org", new=AsyncMock(return_value=True)), \
         patch.object(m, "_find_recent_platform_report", new=AsyncMock(return_value=report)), \
         patch.object(m, "_find_report_by_thread_ts", new=AsyncMock(return_value=report)), \
         patch("app.services.report_service.ReportService") as RS:
        RS.return_value.set_data_sources_for_report = AsyncMock()
        res = await m._process_verified_message(_db(), _adapter(), data, mapping)
        await _settle()

    assert res["action"] == "message_processed"
    assert m.completion_service.create_completion.await_count == 1
    assert (len(teams.opens()) == 1) is bubble


@pytest.mark.asyncio
async def test_the_run_starts_without_waiting_for_teams(teams):
    m = _manager_with_report()
    report = SimpleNamespace(id="R1", title="Chat")
    data = {
        "platform_type": "teams", "message_text": "How many sales?", "thread_ts": CONV,
        "message_ts": "msg-1", "channel_id": CONV, "channel_type": "personal", "is_thread_reply": True,
    }
    mapping = SimpleNamespace(app_user_id="u1", organization_id="org1", external_user_id="ext-1",
                              platform_type="teams", platform_id="plat-1")
    teams.gate = asyncio.Event()      # Teams is slow to answer the bubble request
    with patch("app.core.permission_resolver.principal_belongs_to_org", new=AsyncMock(return_value=True)), \
         patch.object(m, "_find_recent_platform_report", new=AsyncMock(return_value=report)), \
         patch("app.services.report_service.ReportService") as RS:
        RS.return_value.set_data_sources_for_report = AsyncMock()
        res = await asyncio.wait_for(
            m._process_verified_message(_db(), _adapter(), data, mapping), timeout=5,
        )
        assert res["action"] == "message_processed"
        assert m.completion_service.create_completion.await_count == 1
        assert teams.opens() == []    # the bubble is still on its way
        teams.gate.set()
        await _settle()

    assert len(teams.opens()) == 1


@pytest.mark.asyncio
async def test_failed_run_creation_does_not_leave_a_bubble(teams):
    m = _manager_with_report()
    m.completion_service.create_completion = AsyncMock(side_effect=RuntimeError("boom"))
    report = SimpleNamespace(id="R1", title="Chat")
    data = {
        "platform_type": "teams", "message_text": "How many sales?", "thread_ts": CONV,
        "message_ts": "msg-1", "channel_id": CONV, "channel_type": "personal", "is_thread_reply": True,
    }
    mapping = SimpleNamespace(app_user_id="u1", organization_id="org1", external_user_id="ext-1",
                              platform_type="teams", platform_id="plat-1")
    with patch("app.core.permission_resolver.principal_belongs_to_org", new=AsyncMock(return_value=True)), \
         patch.object(m, "_find_recent_platform_report", new=AsyncMock(return_value=report)), \
         patch("app.services.report_service.ReportService") as RS:
        RS.return_value.set_data_sources_for_report = AsyncMock()
        with pytest.raises(RuntimeError):
            await m._process_verified_message(_db(), _adapter(), data, mapping)
        await _settle()

    # The run never started, so no bubble is shown at all.
    assert teams.activities == []
    assert CONV not in teams_stream._conversations

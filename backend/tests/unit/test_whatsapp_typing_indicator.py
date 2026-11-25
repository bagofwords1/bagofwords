"""WhatsApp "typing…" indicator while the agent works.

Drives the real WhatsAppAdapter + whatsapp_typing against a fake Graph API
(httpx.MockTransport — Meta's API is the boundary) and a fake clock, and
asserts what WhatsApp receives: an indicator on the user's message as soon as
the question arrives, refreshed before Meta's 25-second limit for as long as
the chat has a run going, back shortly after each mid-run message, and gone
once the chat's runs have ended — and only those runs' own ends count.
"""
import asyncio
import gc
import json
import weakref
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from app.services.platform_adapters import whatsapp_adapter as whatsapp_adapter_mod
from app.services.platform_adapters import whatsapp_typing
from app.services.platform_adapters.whatsapp_adapter import WhatsAppAdapter

META_DISMISS_SECONDS = 25.0  # Meta drops the indicator after this long
CHAT = "15550001111"
PNID = "PNID_1"


class FakeClock:
    """Time only moves when a test advances it; sleepers wake in order."""

    def __init__(self):
        self.t = 1000.0
        self.waiters = []  # (wake_at, future, task)

    def now(self):
        return self.t

    async def sleep(self, seconds):
        fut = asyncio.get_running_loop().create_future()
        self.waiters.append((self.t + seconds, fut, asyncio.current_task()))
        try:
            await fut
        finally:
            self.waiters = [w for w in self.waiters if w[1] is not fut]

    def _blocked(self):
        return {w[2] for w in self.waiters if not w[1].done()}

    async def settle(self):
        """Run until every live typing loop is done or asleep on this clock."""
        for _ in range(10_000):
            live = [c.task for c in whatsapp_typing._chats.values() if c.task and not c.task.done()]
            if all(t in self._blocked() for t in live):
                # one more turn so a just-finished send is recorded
                await asyncio.sleep(0)
                live = [c.task for c in whatsapp_typing._chats.values() if c.task and not c.task.done()]
                if all(t in self._blocked() for t in live):
                    return
            await asyncio.sleep(0)
        raise AssertionError("typing loops never settled")

    async def advance(self, seconds):
        end = self.t + seconds
        while True:
            await self.settle()
            due = [w for w in self.waiters if w[0] <= end and not w[1].done()]
            if not due:
                break
            wake_at, fut, _ = min(due, key=lambda w: w[0])
            self.t = max(self.t, wake_at)
            fut.set_result(None)
        self.t = end
        await self.settle()


class FakeGraph:
    def __init__(self, clock):
        self.clock = clock
        self.requests = []           # (time, phone_number_id, body)
        self.typing_status = 200     # status for typing requests
        self.typing_error = None     # exception class to raise instead
        self.latency = 0.0           # seconds a typing request takes

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        pnid = request.url.path.strip("/").split("/")[-2]  # .../{phone_number_id}/messages
        if "typing_indicator" in body:
            if self.latency:
                await self.clock.sleep(self.latency)
            self.requests.append((self.clock.t, pnid, body))
            if self.typing_error:
                raise self.typing_error("network down")
            return httpx.Response(self.typing_status, json={"success": self.typing_status == 200})
        self.requests.append((self.clock.t, pnid, body))
        return httpx.Response(200, json={"messages": [{"id": "wamid.OUT"}]})

    def typing(self, pnid=None):
        return [(t, b) for t, p, b in self.requests
                if "typing_indicator" in b and (pnid is None or p == pnid)]


@pytest_asyncio.fixture
async def wa(monkeypatch):
    clock = FakeClock()
    graph = FakeGraph(clock)
    original = httpx.AsyncClient

    class _Client(original):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(graph.handler)
            super().__init__(*a, **kw)

    monkeypatch.setattr(whatsapp_adapter_mod.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(whatsapp_typing, "_now", clock.now)
    monkeypatch.setattr(whatsapp_typing, "_sleep", clock.sleep)
    for name, fresh in (("_chats", {}), ("_runs", {}), ("_bound", {})):
        monkeypatch.setattr(whatsapp_typing, name, fresh)
    monkeypatch.setattr(whatsapp_typing, "_ended_ids", type(whatsapp_typing._ended_ids)())
    monkeypatch.setattr(whatsapp_typing, "_early_ends", type(whatsapp_typing._early_ends)())
    yield SimpleNamespace(clock=clock, graph=graph, adapter=_adapter())
    tasks = [c.task for c in whatsapp_typing._chats.values() if c.task]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _adapter(pnid=PNID):
    platform = SimpleNamespace(
        platform_config={"phone_number_id": pnid},
        decrypt_credentials=lambda: {"access_token": "EAAtoken", "phone_number_id": pnid},
    )
    return WhatsAppAdapter(platform)


def _start(wa, message_id, completion_id=None, chat=CHAT, adapter=None):
    """A question arrives and its run is created (bound to its completion)."""
    adapter = adapter or wa.adapter
    token = whatsapp_typing.start_run(adapter, adapter.phone_number_id(), chat, message_id)
    if completion_id is not None:
        whatsapp_typing.bind_run(token, completion_id)
    return token


def _gaps(times, start, end):
    """Longest stretch in [start, end] with no indicator showing."""
    shown_until, longest = start, 0.0
    for t in sorted(times):
        longest = max(longest, t - shown_until)
        shown_until = max(shown_until, t + META_DISMISS_SECONDS)
    return max(longest, end - shown_until)


@pytest.mark.asyncio
async def test_question_gets_typing_indicator_on_its_message_right_away(wa):
    _start(wa, "wamid.Q1", "c1")
    await wa.clock.advance(0)

    sent = wa.graph.typing()
    assert len(sent) == 1
    body = sent[0][1]
    assert body["messaging_product"] == "whatsapp"
    assert body["message_id"] == "wamid.Q1"
    assert body["typing_indicator"] == {"type": "text"}
    # WhatsApp only accepts the indicator as part of a read receipt.
    assert body["status"] == "read"


@pytest.mark.asyncio
@pytest.mark.parametrize("run_seconds", [30, 95, 600])
@pytest.mark.parametrize("latency", [0.0, 6.0, 15.0])
async def test_indicator_never_lapses_while_the_run_is_going(wa, run_seconds, latency):
    """Even when each Graph round trip is slow, refreshes stay within 25s."""
    wa.graph.latency = latency
    start = wa.clock.t
    _start(wa, "wamid.Q1", "c1")
    await wa.clock.advance(run_seconds)

    times = [t for t, _ in wa.graph.typing()]
    assert times, "indicator never sent"
    assert _gaps(times, start + latency, start + run_seconds) <= 0.01


@pytest.mark.asyncio
async def test_indicator_stops_once_the_run_ends(wa):
    _start(wa, "wamid.Q1", "c1")
    await wa.clock.advance(45)
    whatsapp_typing.end_run(CHAT, "c1")
    count = len(wa.graph.typing())

    await wa.clock.advance(300)
    assert len(wa.graph.typing()) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before-bind", "after-bind"])
async def test_an_end_for_another_completion_does_not_stop_the_indicator(wa, when):
    """A stale row from before a restart being retired reports an end for an
    id this run doesn't own — it must not stop the live run's indicator."""
    token = _start(wa, "wamid.Q1")
    if when == "before-bind":
        whatsapp_typing.end_run(CHAT, "stale-row")
        whatsapp_typing.bind_run(token, "c-live")
    else:
        whatsapp_typing.bind_run(token, "c-live")
        whatsapp_typing.end_run(CHAT, "stale-row")
    await wa.clock.advance(1)
    before = len(wa.graph.typing())
    await wa.clock.advance(120)

    assert len(wa.graph.typing()) > before + 4


@pytest.mark.asyncio
async def test_a_run_that_ends_before_it_is_bound_stops_at_the_bind(wa):
    """A quick run can finish before the webhook gets to bind its id."""
    token = _start(wa, "wamid.Q1")
    await wa.clock.advance(1)
    whatsapp_typing.end_run(CHAT, "c-fast")
    whatsapp_typing.bind_run(token, "c-fast")
    count = len(wa.graph.typing())
    await wa.clock.advance(120)

    assert len(wa.graph.typing()) == count


@pytest.mark.asyncio
async def test_a_run_bound_to_no_id_ends_on_the_chats_next_end(wa):
    token = _start(wa, "wamid.Q1")
    whatsapp_typing.bind_run(token, None)
    await wa.clock.advance(30)
    whatsapp_typing.end_run(CHAT, "whatever")
    count = len(wa.graph.typing())
    await wa.clock.advance(120)

    assert len(wa.graph.typing()) == count


@pytest.mark.asyncio
async def test_indicator_stays_until_every_run_in_the_chat_ended_and_each_counts_once(wa):
    _start(wa, "wamid.Q1", "c1")
    await wa.clock.advance(5)
    _start(wa, "wamid.Q2", "c2")
    await wa.clock.advance(5)

    # The first run's end arrives from both reporters.
    whatsapp_typing.end_run(CHAT, "c1")
    whatsapp_typing.end_run(CHAT, "c1")
    before = len(wa.graph.typing())
    await wa.clock.advance(60)
    assert len(wa.graph.typing()) > before, "stopped while the second run was still going"

    whatsapp_typing.end_run(CHAT, "c2")
    after = len(wa.graph.typing())
    await wa.clock.advance(300)
    assert len(wa.graph.typing()) == after


@pytest.mark.asyncio
async def test_newest_question_carries_the_indicator(wa):
    _start(wa, "wamid.Q1", "c1")
    await wa.clock.advance(7)
    _start(wa, "wamid.Q2", "c2")
    await wa.clock.advance(60)

    sent_after_q2 = [b["message_id"] for t, b in wa.graph.typing() if t >= wa.clock.t - 60]
    assert sent_after_q2 and set(sent_after_q2) == {"wamid.Q2"}


@pytest.mark.asyncio
async def test_one_user_on_two_business_numbers_has_two_independent_chats(wa):
    """The same person can message two of the org's numbers."""
    sales, support = _adapter("PNID_SALES"), _adapter("PNID_SUPPORT")
    _start(wa, "wamid.S1", "c-sales", adapter=sales)
    await wa.clock.advance(2)
    _start(wa, "wamid.F1", "c-support", adapter=support)
    await wa.clock.advance(3)

    # The support answer arrives and its run ends; sales is still working.
    await support.send_response({"to": CHAT, "text": "Support answer"})
    whatsapp_typing.end_run(CHAT, "c-support")
    support_done = wa.clock.t
    await wa.clock.advance(100)

    late_support = [t for t, _ in wa.graph.typing("PNID_SUPPORT") if t > support_done]
    sales_times = [t for t, _ in wa.graph.typing("PNID_SALES")]
    assert not late_support, "typing… reappeared under the finished support answer"
    assert {b["message_id"] for _, b in wa.graph.typing("PNID_SALES")} == {"wamid.S1"}
    assert _gaps(sales_times, sales_times[0], wa.clock.t) <= 0.01


@pytest.mark.asyncio
async def test_chats_are_independent(wa):
    _start(wa, "wamid.A", "ca")
    _start(wa, "wamid.B", "cb", chat="15559998888")
    await wa.clock.advance(5)
    whatsapp_typing.end_run(CHAT, "ca")
    await wa.clock.advance(60)

    late = {b["message_id"] for t, b in wa.graph.typing() if t > wa.clock.t - 60}
    assert late == {"wamid.B"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_refused_indicator_is_not_retried(wa, status):
    wa.graph.typing_status = status
    _start(wa, "wamid.Q1", "c1")
    await wa.clock.advance(300)

    assert len(wa.graph.typing()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [429, 500, 503, httpx.ConnectError, httpx.ReadTimeout])
async def test_a_transient_failure_does_not_turn_the_indicator_off(wa, failure):
    if isinstance(failure, int):
        wa.graph.typing_status = failure
    else:
        wa.graph.typing_error = failure
    _start(wa, "wamid.Q1", "c1")
    await wa.clock.advance(30)
    wa.graph.typing_status, wa.graph.typing_error = 200, None
    await wa.clock.advance(100)

    times = [t for t, _ in wa.graph.typing()]
    assert len(times) >= 6, "gave up after a passing failure"


@pytest.mark.asyncio
async def test_refused_indicator_is_not_restarted_by_a_message(wa):
    wa.graph.typing_status = 400
    _start(wa, "wamid.Q1", "c1")
    await wa.clock.advance(3)
    await wa.adapter.send_response({"to": CHAT, "text": "partial"})
    await wa.clock.advance(60)

    assert len(wa.graph.typing()) == 1


@pytest.mark.asyncio
async def test_indicator_gives_up_at_the_hard_cap_when_the_end_is_never_reported(wa):
    _start(wa, "wamid.Q1", "c1")
    await wa.clock.advance(whatsapp_typing.MAX_RUN_SECONDS + 120)
    await wa.adapter.send_response({"to": CHAT, "text": "late message"})
    await wa.clock.advance(60)

    times = [t for t, _ in wa.graph.typing()]
    assert max(times) - min(times) <= whatsapp_typing.MAX_RUN_SECONDS


@pytest.mark.asyncio
async def test_aborted_run_stops_the_indicator(wa):
    token = _start(wa, "wamid.Q1")
    await wa.clock.advance(1)
    whatsapp_typing.abort_run(token)
    count = len(wa.graph.typing())
    await wa.clock.advance(120)
    assert len(wa.graph.typing()) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("answer_at", [3, 19.5, 39])
async def test_no_indicator_reappears_under_the_final_answer(wa, answer_at):
    """The answer is sent a moment before the run reports its end."""
    _start(wa, "wamid.Q1", "c1")
    await wa.clock.advance(answer_at)
    assert await wa.adapter.send_response({"to": CHAT, "text": "Here is your answer"}) is True
    answer_index = len(wa.graph.requests) - 1
    await wa.clock.advance(2)
    whatsapp_typing.end_run(CHAT, "c1")
    await wa.clock.advance(120)

    # By order, not time: a re-send in the same instant would still show
    # "typing…" under the answer.
    after_answer = wa.graph.requests[answer_index + 1:]
    assert not [b for _, _, b in after_answer if "typing_indicator" in b]


@pytest.mark.asyncio
@pytest.mark.parametrize("sent_after", [1, 10, 19, 33])
async def test_indicator_comes_back_shortly_after_a_mid_run_message(wa, sent_after):
    """A file or partial reply mid-run dismisses the indicator on WhatsApp;
    the run is still going, so it comes back a few seconds later — not at
    the next scheduled refresh."""
    _start(wa, "wamid.Q1", "c1")
    await wa.clock.advance(sent_after)
    assert await wa.adapter.send_response({"to": CHAT, "text": "Here is the CSV"}) is True
    sent_at = wa.clock.t
    await wa.clock.advance(60)

    after = [t for t, _ in wa.graph.typing() if t > sent_at]
    assert after and after[0] - sent_at <= whatsapp_typing.AFTER_SEND_GRACE_SECONDS + 0.01


@pytest.mark.asyncio
@pytest.mark.parametrize("message_times", [[8, 12, 30], [3, 4, 5, 6], [21, 50, 51, 90]])
async def test_mid_run_messages_never_leave_a_long_gap(wa, message_times):
    """However the bot's messages fall, the chat is never without "typing…"
    for longer than the short wait after each message — including after the
    last one."""
    start = wa.clock.t
    _start(wa, "wamid.Q1", "c1")
    outbound = []
    for at in message_times:
        await wa.clock.advance(start + at - wa.clock.t)
        await wa.adapter.send_response({"to": CHAT, "text": f"part {at}"})
        outbound.append(wa.clock.t)
    end = start + message_times[-1] + 40
    await wa.clock.advance(end - wa.clock.t)

    # Walk the timeline: the chat looks idle when it shows no "typing…" (a
    # send shows it for 25s, a message dismisses it) and the bot's last
    # message is older than that moment.
    events = sorted([(t, "typing") for t, _ in wa.graph.typing()] + [(t, "msg") for t in outbound])
    active_until, longest = start, 0.0
    for t, kind in events:
        longest = max(longest, t - active_until)
        active_until = t + META_DISMISS_SECONDS if kind == "typing" else t
    longest = max(longest, end - active_until)
    assert longest <= whatsapp_typing.AFTER_SEND_GRACE_SECONDS + 0.01


@pytest.mark.asyncio
async def test_reaction_does_not_hold_the_indicator_back(wa):
    """A reaction (e.g. an earlier question's ✅) doesn't dismiss "typing…" on
    WhatsApp, so it must not delay the next refresh either."""
    _start(wa, "wamid.Q2", "c2")
    await wa.clock.advance(0)
    first = wa.graph.typing()[0][0]
    await wa.clock.advance(whatsapp_typing.REFRESH_SECONDS - 1)
    assert await wa.adapter.add_reaction(CHAT, "wamid.Q1", "white_check_mark") is True
    await wa.clock.advance(2)

    times = [t for t, _ in wa.graph.typing()]
    assert len(times) == 2 and times[1] - first == whatsapp_typing.REFRESH_SECONDS


@pytest.mark.asyncio
async def test_a_finished_chat_does_not_keep_its_adapter(wa):
    """The adapter holds the number's decrypted credentials: once the chat
    has no run, nothing here may keep it alive."""
    adapter = _adapter("PNID_TMP")
    ref = weakref.ref(adapter)
    whatsapp_typing.bind_run(
        whatsapp_typing.start_run(adapter, "PNID_TMP", CHAT, "wamid.Q1"), "c1",
    )
    await wa.clock.advance(5)
    whatsapp_typing.end_run(CHAT, "c1")
    await wa.clock.advance(1)
    del adapter
    gc.collect()

    assert ref() is None

"""Audit stream exporter semantics (docs/design/audit-log-streams.md, Loop A3).

Events are produced by real audited API actions (API key create/revoke), the
stream is configured through the real API, and the exporter tick is invoked
directly with an explicit ``now`` (no scheduler, no sleeps). The only mocked
boundary is the SIEM intake (tests/mocks/siem_consumer.py), which reports what
arrived; the database is the source of truth it is checked against.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from app.ee.audit.streams import exporter
from app.ee.audit.streams.destinations.http import HttpsDestination
from tests.e2e.audit.conftest import h

pytestmark = pytest.mark.e2e


def _run(coro):
    return asyncio.run(coro)


def _tick(now: datetime):
    return _run(exporter.run_exporter_tick(now=now))


def _later(seconds: float = 90) -> datetime:
    return datetime.utcnow() + timedelta(seconds=seconds)


def _api_key_events(client, admin, n: int) -> None:
    for i in range(n):
        r = client.post("/api/api_keys", json={"name": f"k-{uuid.uuid4().hex[:6]}"}, headers=h(admin["token"], admin["org_id"]))
        assert r.status_code == 200, r.text


def _db_ids(client, admin, action_prefix: str | None = None) -> list[str]:
    ids, page = [], 1
    while True:
        r = client.get("/api/enterprise/audit", params={"page": page, "page_size": 100},
                       headers=h(admin["token"], admin["org_id"]))
        assert r.status_code == 200, r.text
        body = r.json()
        ids += [i["id"] for i in body["items"] if not action_prefix or i["action"].startswith(action_prefix)]
        if page >= body["total_pages"]:
            return ids
        page += 1


def _stream(client, admin, siem, *, dest="https", start_from="beginning", **over):
    payload = {
        "name": f"s-{uuid.uuid4().hex[:6]}",
        "destination": dest,
        "start_from": start_from,
        "config": {"url": f"{siem.url}/https/hook"} if dest == "https" else {"hec_url": f"{siem.url}/splunk"},
        "secrets": {"hmac_secret": "demo-hmac-secret"} if dest == "https" else {"token": "demo-hec-token"},
    }
    payload.update(over)
    r = client.post("/api/enterprise/audit/streams", json=payload, headers=h(admin["token"], admin["org_id"]))
    assert r.status_code == 200, r.text
    return r.json()


def _get(client, admin, sid):
    r = client.get(f"/api/enterprise/audit/streams/{sid}", headers=h(admin["token"], admin["org_id"]))
    assert r.status_code == 200, r.text
    return r.json()


def _delivered_ids(siem, dest="https") -> list[str]:
    return [e["id"] for e in siem.state.envelopes(dest) if e["action"] != "audit_stream.test"]


@pytest.mark.parametrize("n_events", [3, 11])
def test_every_event_reaches_an_active_stream(test_client, bootstrap_admin, siem, n_events):
    admin = bootstrap_admin()
    _api_key_events(test_client, admin, n_events)
    s = _stream(test_client, admin, siem)

    _tick(_later())

    want = _db_ids(test_client, admin)
    stats = siem.state.stats("https", want)
    # audit_stream.created is written in the same commit as the stream, so it
    # is part of the log and must be delivered too.
    assert stats["missing"] == [] and stats["unexpected"] == []
    assert stats["duplicates"] == 0 and stats["format_errors"] == 0
    status = _get(test_client, admin, s["id"])
    assert status["state"] == "active"
    assert status["delivered_count"] == len(want)
    assert status["status"]["pending"] == 0


def test_cursor_spans_multiple_batches_in_order(test_client, bootstrap_admin, siem, monkeypatch):
    monkeypatch.setattr(HttpsDestination, "max_batch", 2)
    admin = bootstrap_admin()
    _api_key_events(test_client, admin, 7)
    _stream(test_client, admin, siem)

    _tick(_later())

    want = _db_ids(test_client, admin)
    stats = siem.state.stats("https", want)
    assert stats["missing"] == [] and stats["duplicates"] == 0 and stats["out_of_order"] == 0


def test_transient_failures_back_off_and_lose_nothing(test_client, bootstrap_admin, siem):
    admin = bootstrap_admin()
    _api_key_events(test_client, admin, 4)
    s = _stream(test_client, admin, siem)
    siem.state.faults["https"] = {"mode": "503", "count": 3, "sleep": None}

    now = _later()
    _tick(now)
    after_first = _get(test_client, admin, s["id"])
    assert after_first["state"] == "active"
    assert after_first["consecutive_failures"] == 1
    assert after_first["last_error"]
    assert _delivered_ids(siem) == []
    # A tick before the backoff expires does not send.
    requests_before = siem.state.requests.get("https", 0)
    _tick(now + timedelta(seconds=1))
    assert siem.state.requests.get("https", 0) == requests_before

    for i in range(1, 5):
        _tick(now + timedelta(minutes=2 * i))

    want = _db_ids(test_client, admin)
    stats = siem.state.stats("https", want)
    assert stats["missing"] == [] and stats["duplicates"] == 0
    final = _get(test_client, admin, s["id"])
    assert final["state"] == "active" and final["consecutive_failures"] == 0 and final["last_error"] is None


def test_rejected_credentials_stop_the_stream_and_resume_without_gaps(test_client, bootstrap_admin, siem):
    admin = bootstrap_admin()
    _api_key_events(test_client, admin, 2)
    s = _stream(test_client, admin, siem, dest="splunk", secrets={"token": "wrong-token"})

    now = _later()
    _tick(now)
    st = _get(test_client, admin, s["id"])
    assert st["state"] == "invalid"
    assert "403" in (st["last_error"] or "")

    # Events keep being written while the stream is down.
    _api_key_events(test_client, admin, 3)
    _tick(now + timedelta(minutes=5))  # an invalid stream is not retried
    assert _delivered_ids(siem, "splunk") == []

    r = test_client.patch(
        f"/api/enterprise/audit/streams/{s['id']}",
        json={"secrets": {"token": "demo-hec-token"}, "state": "active"},
        headers=h(admin["token"], admin["org_id"]),
    )
    assert r.status_code == 200, r.text
    _tick(_later(180))

    want = _db_ids(test_client, admin)
    stats = siem.state.stats("splunk", want)
    # Includes the events written while invalid and the state-change audit row.
    assert stats["missing"] == [] and stats["duplicates"] == 0
    actions = {e["action"] for e in siem.state.envelopes("splunk")}
    assert "audit_stream.state_changed" in actions
    assert _get(test_client, admin, s["id"])["state"] == "active"


def test_action_filter_delivers_matching_events_and_advances_past_the_rest(test_client, bootstrap_admin, siem):
    admin = bootstrap_admin()
    _api_key_events(test_client, admin, 3)
    s = _stream(test_client, admin, siem, action_filter=["api_key."])

    _tick(_later())

    want = _db_ids(test_client, admin, "api_key.")
    got = _delivered_ids(siem)
    assert sorted(got) == sorted(want) and len(want) >= 3
    assert all(e["action"].startswith("api_key.") for e in siem.state.envelopes("https"))
    assert _get(test_client, admin, s["id"])["status"]["pending"] == 0


def test_streams_never_receive_another_organizations_events(test_client, bootstrap_admin, siem):
    a = bootstrap_admin("orga")
    b = bootstrap_admin("orgb")
    _api_key_events(test_client, a, 2)
    _api_key_events(test_client, b, 4)
    _stream(test_client, a, siem)

    _tick(_later())

    got = set(_delivered_ids(siem))
    assert got == set(_db_ids(test_client, a))
    assert got.isdisjoint(_db_ids(test_client, b))


def test_start_from_now_skips_history_and_beginning_backfills(test_client, bootstrap_admin, siem):
    admin = bootstrap_admin()
    _api_key_events(test_client, admin, 3)
    history = set(_db_ids(test_client, admin))

    now_stream = _stream(test_client, admin, siem, dest="splunk", start_from="now")
    _api_key_events(test_client, admin, 2)
    back_stream = _stream(test_client, admin, siem, dest="https", start_from="beginning")

    _tick(_later())

    from_now = set(_delivered_ids(siem, "splunk"))
    backfill = set(_delivered_ids(siem, "https"))
    assert from_now and from_now.isdisjoint(history)
    assert history <= backfill
    assert now_stream["start_from"] == "now" and back_stream["start_from"] == "beginning"


@pytest.mark.parametrize("delay", [timedelta(seconds=5), timedelta(minutes=10), timedelta(days=2)])
def test_row_committed_after_newer_rows_were_delivered_is_not_skipped(test_client, bootstrap_admin, siem, delay):
    """A transaction that commits late (lock contention, a slow request, a
    replayed spill) makes a row visible after newer rows were already sent,
    with a created_at older than them. Delivery order follows visibility, so
    it is still delivered — however large the delay."""
    from app.dependencies import async_session_maker
    from app.ee.audit.models import AuditLog

    admin = bootstrap_admin()
    _stream(test_client, admin, siem)
    _api_key_events(test_client, admin, 2)
    _tick(_later())
    first = set(_delivered_ids(siem))
    assert first

    async def insert_late():
        # Direct write: the API cannot produce a row whose created_at predates
        # rows that are already visible, which is exactly the shape under test.
        async with async_session_maker() as s:
            row = AuditLog(id=str(uuid.uuid4()), organization_id=admin["org_id"], action="tool.data_queried",
                           resource_type="data_source", created_at=datetime.utcnow() - delay)
            s.add(row)
            await s.commit()
            return row.id

    late = _run(insert_late())
    _tick(_later(120))

    got = _delivered_ids(siem)
    assert late in got
    stats = siem.state.stats("https", _db_ids(test_client, admin))
    assert stats["missing"] == [] and stats["duplicates"] == 0


def test_paused_stream_is_not_delivered_and_resumes_from_its_cursor(test_client, bootstrap_admin, siem):
    admin = bootstrap_admin()
    s = _stream(test_client, admin, siem)
    _tick(_later())
    first = set(_delivered_ids(siem))

    r = test_client.patch(f"/api/enterprise/audit/streams/{s['id']}", json={"state": "inactive"},
                          headers=h(admin["token"], admin["org_id"]))
    assert r.status_code == 200 and r.json()["state"] == "inactive"
    _api_key_events(test_client, admin, 3)
    _tick(_later(120))
    assert set(_delivered_ids(siem)) == first

    r = test_client.patch(f"/api/enterprise/audit/streams/{s['id']}", json={"state": "active"},
                          headers=h(admin["token"], admin["org_id"]))
    assert r.status_code == 200 and r.json()["state"] == "active"
    _tick(_later(240))
    stats = siem.state.stats("https", _db_ids(test_client, admin))
    assert stats["missing"] == [] and stats["duplicates"] == 0


def test_concurrent_exporters_claim_a_stream_once(test_client, bootstrap_admin, siem):
    """Two exporters (e.g. two hosts on one database) racing for the same
    stream: the compare-and-set lease lets exactly one send each batch."""
    admin = bootstrap_admin()
    _api_key_events(test_client, admin, 5)
    s = _stream(test_client, admin, siem)
    now = _later()
    _run(exporter.stamp_visible_rows(now))

    async def race():
        return await asyncio.gather(*(exporter.deliver_stream(s["id"], now) for _ in range(4)))

    sent = _run(race())
    assert sorted(sent)[-1] > 0 and sum(1 for n in sent if n) == 1
    stats = siem.state.stats("https", _db_ids(test_client, admin))
    assert stats["missing"] == [] and stats["duplicates"] == 0


def test_tool_events_written_late_still_reach_the_stream(test_client, bootstrap_admin, siem, monkeypatch, tmp_path):
    """Tool-audit events can reach the table long after they happened (queue
    backpressure, or a disk spill replayed on the next start). They must not
    land behind a cursor that has already moved on."""
    from types import SimpleNamespace

    import app.ee.audit.tool_audit as ta

    monkeypatch.setenv("BOW_AUDIT_SPILL_DIR", str(tmp_path / "spill"))
    admin = bootstrap_admin()
    s = _stream(test_client, admin, siem)
    ctx = {"organization": SimpleNamespace(id=admin["org_id"]), "user": None, "agent_execution_id": "run-late"}

    async def outage_then_spill():
        async def down(events):
            raise ConnectionError("database unavailable")
        monkeypatch.setattr(ta, "_write_batch", down)
        monkeypatch.setattr(ta, "_RETRY_DELAYS_SECONDS", ())
        await ta.start_tool_audit_worker()
        for i in range(3):
            await ta.log_tool_audit(ctx, "tool.data_queried", "data_source", None, {"i": i})
        await ta.stop_tool_audit_worker(timeout=5)
        monkeypatch.undo()
        monkeypatch.setenv("BOW_AUDIT_SPILL_DIR", str(tmp_path / "spill"))

    _run(outage_then_spill())
    assert ta.get_tool_audit_queue_stats()["spilled"] >= 3

    # Other activity moves the cursor forward before the replay happens.
    _api_key_events(test_client, admin, 2)
    _tick(_later(120))
    assert _get(test_client, admin, s["id"])["status"]["pending"] == 0

    # Treat any delay as "late" so the replayed rows record their original time.
    monkeypatch.setattr(ta, "_LATE_WRITE_SECONDS", 0.0)
    assert _run(ta.replay_spilled_tool_audit_events()) == 3
    _tick(_later(600))

    late = [e for e in siem.state.envelopes("https") if e["actor"]["type"] == "agent"]
    assert len(late) == 3
    stats = siem.state.stats("https", _db_ids(test_client, admin))
    assert stats["missing"] == [] and stats["duplicates"] == 0
    # The original event time is preserved for the late writes.
    assert all(e["metadata"].get("occurred_at") for e in late)


def test_racing_stampers_give_every_row_one_unique_sequence(test_client, bootstrap_admin, siem):
    """Several workers run the tick at once (APScheduler fires it in every
    worker). The stamper lease lets one of them number the rows; no row gets
    two numbers and no number is used twice."""
    from sqlalchemy import func, select

    from app.dependencies import async_session_maker
    from app.ee.audit.models import AuditLog

    admin = bootstrap_admin()
    _stream(test_client, admin, siem)
    _api_key_events(test_client, admin, 6)
    now = _later()

    async def race():
        return await asyncio.gather(*(exporter.stamp_visible_rows(now) for _ in range(4)))

    stamped = _run(race())
    assert sum(1 for n in stamped if n) == 1

    async def check():
        async with async_session_maker() as s:
            total, seqs, distinct, nulls = (await s.execute(
                select(func.count(AuditLog.id), func.count(AuditLog.export_seq),
                       func.count(func.distinct(AuditLog.export_seq)),
                       func.count(AuditLog.id).filter(AuditLog.export_seq.is_(None)))
                .where(AuditLog.organization_id == admin["org_id"])
            )).one()
            return total, seqs, distinct, nulls

    total, seqs, distinct, nulls = _run(check())
    assert nulls == 0 and seqs == total == distinct

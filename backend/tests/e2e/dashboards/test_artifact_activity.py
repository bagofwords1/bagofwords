"""The app activity log (``POST /api/artifacts/{id}/runtime/activity``,
services/artifact_activity.py, the ``read_artifact_activity`` tool).

The contract:

* only someone who edits the app, in an app session, is recorded; readers,
  anonymous callers and embed sessions never are, and are told nothing;
* events are reduced to known kinds and fields and bounded in size;
* retention: older than RETENTION_DAYS goes, and only the newest
  KEEP_PER_REPORT per conversation stay;
* the agent gets each failure once, automatically, with the steps before
  it; the tool reads everything back, by artifact or by event.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta

import jwt
import pytest
from sqlalchemy import text

from app.dependencies import async_session_maker
from app.services import artifact_activity
from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

pytestmark = pytest.mark.e2e

DASH = "conv_three_dash/dash_sales"


def path(aid):
    return f"/api/artifacts/{aid}/runtime/activity"


def batch(*events, session=None):
    return {"session_id": session or uuid.uuid4().hex, "events": list(events)}


def report_of(rows, aid) -> str:
    with rows.engine.begin() as conn:
        return str(conn.execute(text("select report_id from artifacts where id = :id"), {"id": aid}).scalar_one())


def stored(rows, aid) -> list[dict]:
    with rows.engine.begin() as conn:
        result = conn.execute(text(
            "select kind, op, status, error_code, source, user_id, report_id, detail from artifact_activity_events "
            "where artifact_id = :id order by created_at, seq"), {"id": aid})
        rows = [dict(r._mapping) for r in result]
    for row in rows:  # JSON comes back as text from a raw query on SQLite
        if isinstance(row["detail"], str):
            row["detail"] = json.loads(row["detail"])
    return rows


def run(coro):
    return asyncio.run(coro)


FAILED_SEND = {"kind": "request", "op": "conversations.create", "status": "error",
               "error_code": "conversation.agent_forbidden", "duration_ms": 41,
               "detail": {"agents": ["music-store"], "message": "You can't use that agent here"}}


def test_an_editor_s_activity_is_recorded_for_their_conversation(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    r = test_client.post(path(aid), json=batch(
        {"kind": "click", "seq": 1, "detail": {"target": "button 'New conversation'"}},
        {**FAILED_SEND, "seq": 2},
    ), headers=as_(corpus, "owner"))
    assert r.status_code == 200 and r.json()["stored"] == 2
    rows = stored(corpus_rows, aid)
    assert [e["kind"] for e in rows] == ["click", "request"]
    assert all(e["source"] == "editor" and e["user_id"] == corpus.cast.id("owner") for e in rows)
    assert {e["report_id"] for e in rows} == {report_of(corpus_rows, aid)}
    assert rows[1]["error_code"] == "conversation.agent_forbidden"


def test_the_editor_reads_the_log_back_newest_first(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    owner = as_(corpus, "owner")
    test_client.post(path(aid), json=batch(
        {"kind": "click", "seq": 1, "detail": {"target": "button 'Send'"}}, {**FAILED_SEND, "seq": 2}), headers=owner)

    r = test_client.get(path(aid), headers=owner)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [e["kind"] for e in body["events"]] == ["request", "click"]  # newest first
    failed, click = body["events"]
    assert failed["failed"] is True and failed["source"] == "editor"
    assert failed["error_code"] == "conversation.agent_forbidden"
    assert failed["text"].startswith("conversations.create") and "You can't use that agent here" in failed["text"]
    assert click["failed"] is False and click["text"] == "clicked button 'Send'"
    assert failed["created_at"].endswith("Z")
    assert body["retention_days"] == artifact_activity.RETENTION_DAYS
    only = test_client.get(path(aid), params={"only_errors": "true"}, headers=owner).json()["events"]
    assert [e["id"] for e in only] == [failed["id"]]
    assert test_client.get(path(aid), params={"source": "preview"}, headers=owner).json()["events"] == []
    # Readers of the published app, anonymous callers and other orgs are told nothing.
    assert test_client.get(path(aid), headers=as_(corpus, "member")).status_code == 404
    assert test_client.get(path(aid), headers=as_(corpus, "anonymous")).status_code == 401
    assert test_client.get(path(aid), headers=as_(corpus, "other_org")).status_code in (403, 404)


BIG_RESPONSE = {"columns": [{"field": "name"}, {"field": "total"}],
                "rows": [{"name": f"customer {i} with a long name " * 3, "total": i} for i in range(1500)]}
STREAM = [{"type": "started", "message_id": "m1"}, {"type": "text_delta", "block": 0, "text": "Iron Maiden leads"},
          {"type": "result", "id": "r1", "columns": [{"field": "artist"}], "rows": [{"artist": "Iron Maiden"}]},
          {"type": "completed", "status": "success"}]


def with_payload(session=None):
    return batch(
        {"kind": "request", "op": "records.query", "status": "ok", "duration_ms": 58, "seq": 1,
         "detail": {"resource": "orders", "action": "query"},
         "payload": {"request": {"method": "records", "resource": "orders", "action": "query", "filters": {"region": "north"}},
                     "response": {"columns": [{"field": "id"}], "rows": [{"id": 1}, {"id": 2}]}}},
        {"kind": "request", "op": "records.query", "status": "ok", "duration_ms": 900, "seq": 2,
         "detail": {"resource": "customers", "action": "query"}, "payload": {"response": BIG_RESPONSE}},
        {"kind": "stream", "op": "conversations.send", "status": "completed", "duration_ms": 1300, "seq": 3,
         "payload": {"request": {"method": "conversationSend", "text": "who leads?"}, "stream": STREAM}},
        {"kind": "click", "seq": 4, "detail": {"target": "button 'Send'"}, "payload": {"request": {"never": "stored"}}},
        session=session)


def grant_edit(test_client, corpus, aid, person):
    r = test_client.put(f"/api/artifacts/{aid}/sharing", json={"visibility": "internal",
                        "editor_user_ids": [corpus.cast.id(person)]}, headers=as_(corpus, "owner"))
    assert r.status_code == 200, r.text


def test_payloads_are_stored_bounded_encrypted_and_private_to_their_owner(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    grant_edit(test_client, corpus, aid, "member")
    owner, member = as_(corpus, "owner"), as_(corpus, "member")
    assert test_client.post(path(aid), json=with_payload(), headers=owner).json()["stored"] == 4

    rows = test_client.get(path(aid), headers=owner).json()["events"]
    by_seq = {e["text"]: e for e in rows}
    small = next(e for e in rows if e["op"] == "records.query" and e["duration_ms"] == 58)
    big = next(e for e in rows if e["op"] == "records.query" and e["duration_ms"] == 900)
    stream = next(e for e in rows if e["kind"] == "stream")
    click = next(e for e in rows if e["kind"] == "click")
    assert small["has_payload"] and small["payload_mine"] and stream["has_payload"]
    assert not click["has_payload"]  # clicks carry nothing, whatever the client sends

    got = test_client.get(f"{path(aid)}/{small['id']}/payload", headers=owner).json()
    assert got["request"]["filters"] == {"region": "north"} and got["response"]["rows"] == [{"id": 1}, {"id": 2}]
    assert got["stream"] is None
    got = test_client.get(f"{path(aid)}/{stream['id']}/payload", headers=owner).json()
    assert [e["type"] for e in got["stream"]] == ["started", "text_delta", "result", "completed"]
    assert got["stream"][1]["text"] == "Iron Maiden leads" and got["stream"][2]["rows"] == 1  # a result keeps its count
    bounded = test_client.get(f"{path(aid)}/{big['id']}/payload", headers=owner).json()["response"]
    assert bounded["truncated"]["rows_total"] == 1500 and 0 < bounded["truncated"]["rows_kept"] < 1500
    assert len(json.dumps(bounded)) <= artifact_activity.MAX_PAYLOAD_CHARS

    # Encrypted at rest like step results: with encryption active the raw
    # column holds ciphertext envelopes, never the words.
    from app.ee.encryption.types import encryption_active, is_encrypted_envelope
    with corpus_rows.engine.begin() as conn:
        raw = conn.execute(text("select request, response, stream from artifact_activity_payloads")).fetchall()
    assert raw
    if encryption_active():
        assert all("north" not in str(r) and "Iron Maiden" not in str(r) for r in raw)
        assert all(is_encrypted_envelope(json.loads(c)) for r in raw for c in r if c is not None)

    # A co-editor sees the events, and that a payload exists and whose it is, never the payload.
    theirs = test_client.get(path(aid), headers=member).json()["events"]
    row = next(e for e in theirs if e["id"] == small["id"])
    assert row["has_payload"] and not row["payload_mine"] and row["user_name"]
    assert test_client.get(f"{path(aid)}/{small['id']}/payload", headers=member).status_code == 404
    # And theirs is theirs: the owner cannot read it either.
    test_client.post(path(aid), json=with_payload(), headers=member)
    mine = next(e for e in test_client.get(path(aid), headers=member).json()["events"] if e["payload_mine"])
    assert test_client.get(f"{path(aid)}/{mine['id']}/payload", headers=member).status_code == 200
    assert test_client.get(f"{path(aid)}/{mine['id']}/payload", headers=owner).status_code == 404
    assert test_client.get(f"{path(aid)}/{small['id']}/payload", headers=as_(corpus, "anonymous")).status_code == 401


def test_payloads_expire_after_a_day_while_their_events_stay(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    owner = as_(corpus, "owner")
    test_client.post(path(aid), json=with_payload(), headers=owner)
    old = (datetime.utcnow() - timedelta(hours=artifact_activity.PAYLOAD_RETENTION_HOURS + 1)).isoformat(sep=" ")
    with corpus_rows.engine.begin() as conn:
        assert conn.execute(text("select count(*) from artifact_activity_payloads")).scalar_one() == 3
        conn.execute(text("update artifact_activity_payloads set created_at = :t"), {"t": old})
    test_client.post(path(aid), json=batch({"kind": "click", "detail": {"target": "button 'Next'"}}), headers=owner)  # prunes
    with corpus_rows.engine.begin() as conn:
        assert conn.execute(text("select count(*) from artifact_activity_payloads")).scalar_one() == 0
        assert conn.execute(text("select count(*) from artifact_activity_events")).scalar_one() == 5
    assert all(not e["has_payload"] for e in test_client.get(path(aid), headers=owner).json()["events"])


def test_the_agent_gets_a_payload_only_as_the_owner_and_the_data_gate_allow(corpus, corpus_rows, test_client):
    from app.ai.tools.implementations.read_artifact_activity import ReadArtifactActivityTool
    from app.models.report import Report
    from app.models.user import User

    aid = own(corpus_rows, corpus, DASH)
    report_id = report_of(corpus_rows, aid)
    publish(aid)
    grant_edit(test_client, corpus, aid, "member")
    test_client.post(path(aid), json=with_payload(), headers=as_(corpus, "owner"))
    stream_id = next(e["id"] for e in test_client.get(path(aid), headers=as_(corpus, "owner")).json()["events"]
                     if e["kind"] == "stream")

    async def call(person, settings, **args):
        async with async_session_maker() as db:
            report = await db.get(Report, report_id)
            user = await db.get(User, corpus.cast.id(person))
            out = None
            ctx = {"db": db, "report": report, "user": user, "organization_settings": settings}
            async for event in ReadArtifactActivityTool().run_stream(args, ctx):
                if event.type == "tool.end":
                    out = event.payload["output"]
            return out

    allowed, hidden = {"allow_llm_see_data": {"value": True}}, {"allow_llm_see_data": {"value": False}}
    listed = run(call("owner", allowed))["events"]
    assert sum(1 for e in listed if e.get("has_payload")) == 3
    # The owner, data allowed: the payload itself.
    event = run(call("owner", allowed, event_id=stream_id))["event"]
    assert event["payload"]["request"]["text"] == "who leads?" and event["payload"]["stream"][1]["text"] == "Iron Maiden leads"
    # Data hidden from the model: the shape only, as for step results.
    event = run(call("owner", hidden, event_id=stream_id))["event"]
    assert "payload" not in event
    assert event["payload_shape"]["request"] == {"method": "str(16)", "text": "str(10)"}
    assert event["payload_shape"]["stream"] == {"events": 4, "types": {"started": 1, "text_delta": 1, "result": 1, "completed": 1},
                                                "text_chars": 17, "ended": "completed"}
    # Another editor's turn: shape, and that it is not theirs -- whatever the gate says.
    event = run(call("member", allowed, event_id=stream_id))["event"]
    assert "payload" not in event and event["payload_private"] is True and event["payload_shape"]["stream"]["events"] == 4
    # The automatic section says where a payload exists.
    async def section():
        async with async_session_maker() as db:
            return await artifact_activity.context_section(db, report_id=report_id, mark=False)
    test_client.post(path(aid), json=batch({**FAILED_SEND, "payload": {"response": {"http_status": 403, "body": {"error_code": "x"}}}}),
                     headers=as_(corpus, "owner"))
    assert "+payload" in run(section())


def test_readers_anonymous_callers_and_embed_sessions_are_never_recorded(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    one = batch({"kind": "click", "detail": {"target": "button 'Send'"}})
    # A reader of the published app: as if there were nothing to record.
    reader = test_client.post(path(aid), json=one, headers=as_(corpus, "member"))
    assert reader.status_code == 404
    assert test_client.post(path(aid), json=one, headers=as_(corpus, "anonymous")).status_code == 401
    assert test_client.post(path(aid), json=one, headers=as_(corpus, "other_org")).status_code in (403, 404)

    admin = as_(corpus, "admin")
    test_client.put(f"/api/organizations/{corpus.cast.org_id}/members/{corpus.cast.people['owner'].membership_id}",
                    json={"custom_attributes": {"employee_id": "E-7"}}, headers=admin)
    host = test_client.post("/api/host_applications", json={
        "name": "Back office", "allowed_origins": ["https://erp.example.com"], "identity_mode": "assertion",
        "identity_source": "member_attribute:employee_id", "issuer": "erp"}, headers=admin).json()
    test_client.put(f"/api/artifacts/{aid}/embedding", json={"host_application_ids": [host["id"]]},
                    headers=as_(corpus, "owner"))
    now = int(time.time())
    assertion = jwt.encode({"iss": "erp", "aud": host["public_id"], "sub": "E-7", "iat": now, "exp": now + 60,
                            "jti": uuid.uuid4().hex}, host["private_key"], algorithm="RS256")
    session = test_client.post(f"/api/embed/{host['public_id']}/session", json={"assertion": assertion})
    assert session.status_code == 200
    # Even the app's own editor, inside a host application, is a viewer there.
    embedded = test_client.post(path(aid), json=one, headers={"Authorization": f"Bearer {session.json()['token']}"})
    assert embedded.status_code == 401
    assert stored(corpus_rows, aid) == []


def test_events_are_reduced_to_known_fields_and_bounded(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    owner = as_(corpus, "owner")
    r = test_client.post(path(aid), json=batch(
        {"kind": "keystrokes", "detail": {"value": "secret"}},                       # unknown kind
        {"kind": "click", "detail": {"target": "button 'Save'", "value": "typed text"}},  # value dropped
        {"kind": "error", "detail": {"message": "x" * 5000, "stack": "y" * 5000}},
        {"kind": "request", "op": "records.update", "status": "weird", "duration_ms": -5},
    ), headers=owner)
    assert r.status_code == 200 and r.json()["stored"] == 3
    click, error, request = stored(corpus_rows, aid)
    assert click["detail"] == {"target": "button 'Save'"}
    assert len(error["detail"]["message"]) <= artifact_activity.MAX_TEXT
    assert len(error["detail"]["stack"]) <= 1200
    assert request["status"] is None
    too_many = batch(*[{"kind": "click"}] * (artifact_activity.MAX_BATCH + 1))
    assert test_client.post(path(aid), json=too_many, headers=owner).status_code == 422
    bad_session = {"session_id": "../../etc", "events": []}
    assert test_client.post(path(aid), json=bad_session, headers=owner).status_code == 422


def test_retention_keeps_recent_events_and_a_bounded_number_per_conversation(corpus, corpus_rows, test_client,
                                                                          monkeypatch):
    aid = own(corpus_rows, corpus, DASH)
    owner = as_(corpus, "owner")
    monkeypatch.setattr(artifact_activity, "KEEP_PER_REPORT", 30)
    test_client.post(path(aid), json=batch({"kind": "click", "detail": {"target": "old"}}), headers=owner)
    with corpus_rows.engine.begin() as conn:
        conn.execute(text("update artifact_activity_events set created_at = :t where artifact_id = :id"),
                     {"t": datetime.utcnow() - timedelta(days=artifact_activity.RETENTION_DAYS + 1), "id": aid})
    for n in range(4):
        events = [{"kind": "click", "seq": n * 10 + i, "detail": {"target": f"b{n * 10 + i}"}} for i in range(10)]
        assert test_client.post(path(aid), json=batch(*events), headers=owner).status_code == 200
    rows = stored(corpus_rows, aid)
    targets = [e["detail"]["target"] for e in rows]
    assert "old" not in targets
    assert len(rows) == 30
    assert "b39" in targets  # the newest stay


def test_the_agent_sees_each_failure_once_with_the_steps_before_it(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    report_id = report_of(corpus_rows, aid)
    owner = as_(corpus, "owner")
    test_client.post(path(aid), json=batch(
        {"kind": "request", "op": "conversations.list", "status": "ok", "seq": 1},
        {"kind": "click", "seq": 2, "detail": {"target": "button 'New conversation'"}},
        {**FAILED_SEND, "seq": 3},
    ), headers=owner)

    async def section(completion_id):
        async with async_session_maker() as db:
            return await artifact_activity.context_section(db, report_id=report_id, completion_id=completion_id)

    first = run(section(str(uuid.uuid4())))
    assert first and "conversation.agent_forbidden" in first and "New conversation" in first
    assert first.index("New conversation") < first.index("conversation.agent_forbidden")
    assert run(section(str(uuid.uuid4()))) is None  # already shown

    # Successes alone never add a section; a new failure does.
    test_client.post(path(aid), json=batch({"kind": "request", "op": "records.list", "status": "ok"}), headers=owner)
    assert run(section(str(uuid.uuid4()))) is None
    test_client.post(path(aid), json=batch({"kind": "error", "detail": {"message": "Cannot read properties of undefined"}}),
                     headers=owner)
    again = run(section(str(uuid.uuid4())))
    assert again and "Cannot read properties of undefined" in again and "agent_forbidden" not in again


def test_the_tool_reads_a_conversation_s_activity(corpus, corpus_rows, test_client):
    from app.ai.tools.implementations.read_artifact_activity import ReadArtifactActivityTool
    from app.models.report import Report

    aid = own(corpus_rows, corpus, DASH)
    report_id = report_of(corpus_rows, aid)
    test_client.post(path(aid), json=batch(
        {"kind": "click", "detail": {"target": "button 'Send'"}}, FAILED_SEND), headers=as_(corpus, "owner"))
    with corpus_rows.engine.begin() as conn:
        version_id = str(conn.execute(text("select id from artifact_versions where artifact_id = :id limit 1"),
                                      {"id": aid}).scalar_one())

    async def call(**args):
        async with async_session_maker() as db:
            report = await db.get(Report, report_id)
            out = None
            async for event in ReadArtifactActivityTool().run_stream(args, {"db": db, "report": report}):
                if event.type == "tool.end":
                    out = event.payload["output"]
            return out

    everything = run(call())
    assert len(everything["events"]) == 2 and everything["failures"] == 1
    # The agent knows version ids; they name the same app.
    assert len(run(call(artifact_id=version_id, only_errors=True))["events"]) == 1
    failed = next(e for e in everything["events"] if e["failed"])
    detail = run(call(event_id=failed["id"][:8]))["event"]
    assert detail["error_code"] == "conversation.agent_forbidden" and detail["detail"]["agents"] == ["music-store"]
    assert run(call(source="preview"))["events"] == []

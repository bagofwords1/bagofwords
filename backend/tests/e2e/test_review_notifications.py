"""E2E: review notifications say what happened, link to where it is fixed, and
close when the underlying review item closes.

Contract:
1. A real schema change (connection refresh) notifies the agent's managers with
   the agent + connection names and a structured diff, linked to the agent's
   tables (and to the item in the Review feed).
2. Consecutive changes keep the earlier ones as history instead of overwriting.
3. Every review notification has a link, including a global instruction
   suggestion (no agent), which links to the instruction itself.
4. When the review item resolves (any path) or is dismissed from the Review
   feed, every recipient's notification shows it, read and "resolved"/"dismissed".
5. If the condition comes back, the notification re-opens as unread.
"""
import asyncio
import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from app.data_sources.clients.sqlite_client import SqliteClient
from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.connection_table import ConnectionTable
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.domain_connection import domain_connection
from app.models.review_item import (
    TYPE_INSTRUCTION_SUGGESTION, TYPE_LOW_CONFIDENCE, SEVERITY_WARNING,
)
from app.services.connection_service import ConnectionService

pytestmark = pytest.mark.e2e


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def table(name, *columns):
    return {"name": name, "columns": [{"name": c, "dtype": t} for c, t in columns], "pks": [], "fks": []}


@pytest.fixture
def two_admins(test_client, create_user, login_user, whoami):
    """Two full admins of one org — both are recipients of every review signal."""
    email = f"revn_{uuid.uuid4().hex[:8]}@test.com"
    create_user(email=email, password="Test1234!")
    token = login_user(email, "Test1234!")
    org_id = whoami(token)["organizations"][0]["id"]

    email2 = f"revn2_{uuid.uuid4().hex[:8]}@test.com"
    r = test_client.post(f"/api/organizations/{org_id}/members",
                         json={"organization_id": org_id, "email": email2, "role": "admin"},
                         headers=_h(token, org_id))
    assert r.status_code == 200, r.json()
    create_user(email=email2, password="Test1234!")
    token2 = login_user(email2, "Test1234!")
    return org_id, token, token2


@pytest.fixture
def no_reliability_loop(monkeypatch):
    """The schema change also kicks the agent-reliability loop as a detached
    background task. It is not under test here and would outlive the test, so
    record the call instead of starting it."""
    from app.services.agent_reliability_service import AgentReliabilityService
    calls = []
    monkeypatch.setattr(AgentReliabilityService, "schedule", lambda self, **kw: calls.append(kw))
    return calls


async def _seed_agent(org_id, agent_name, conn_name):
    # A persisted, already-synced catalog is the baseline a later refresh diffs
    # against; no endpoint can create an arbitrary historical snapshot.
    async with async_session_maker() as db:
        c = Connection(name=conn_name, type="sqlite", config={"database": ":memory:"},
                       organization_id=org_id, auth_policy="system_only",
                       last_synced_at=datetime(2025, 1, 1))
        db.add(c)
        await db.flush()
        ds = DataSource(name=agent_name, organization_id=org_id, is_active=True)
        db.add(ds)
        await db.flush()
        await db.execute(domain_connection.insert().values(data_source_id=ds.id, connection_id=c.id))
        for t, active in ((table("orders", ("id", "int"), ("coupon", "text")), True),
                          (table("customers", ("id", "int"), ("region", "varchar")), True),
                          (table("legacy", ("id", "int")), True)):
            ct = ConnectionTable(connection_id=c.id, no_rows=0, **t)
            db.add(ct)
            await db.flush()
            db.add(DataSourceTable(datasource_id=ds.id, connection_table_id=ct.id,
                                   is_active=active, no_rows=0, **t))
        await db.commit()
        return str(c.id), str(ds.id)


async def _refresh(cid):
    async with async_session_maker() as db:
        await ConnectionService().refresh_schema(db, await db.get(Connection, cid))


def _inbox(test_client, token, org_id):
    r = test_client.get("/api/notifications", headers=_h(token, org_id))
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _for(items, *, type, agent_id=None):
    return [n for n in items if n["type"] == type
            and (agent_id is None or n["subject"].get("data_source_id") == agent_id)]


def test_schema_change_notification_carries_names_diff_and_links(
    test_client, two_admins, no_reliability_loop, monkeypatch,
):
    org_id, token, token2 = two_admins
    agent, conn = f"Agent {uuid.uuid4().hex[:5]}", f"Warehouse {uuid.uuid4().hex[:5]}"
    cid, ds_id = asyncio.run(_seed_agent(org_id, agent, conn))

    async def discover(self, **kwargs):
        return [table("orders", ("id", "int"), ("discount", "numeric")),       # +discount −coupon
                table("customers", ("id", "int"), ("region", "text")),         # varchar → text
                table("refunds", ("id", "int"))]                               # new; legacy gone
    monkeypatch.setattr(SqliteClient, "aget_schemas", discover)
    asyncio.run(_refresh(cid))

    assert no_reliability_loop, "a real schema change still triggers the reliability loop"
    for tok in (token, token2):
        [n] = _for(_inbox(test_client, tok, org_id), type="schema_changed", agent_id=ds_id)
        assert n["link"] == f"/agents/{ds_id}/tables"
        assert agent in n["title"] and conn in n["title"]
        s = n["subject"]
        assert s["review_link"] == f"/agents/{ds_id}/review/{s['review_item_id']}"
        assert s["connection"]["name"] == conn
        diff = s["diff"]
        assert "legacy" in diff["removed_tables"]
        changed = {c["table"]: c for c in diff["changed"]}
        assert {"name": "discount", "type": "numeric"} in changed["orders"]["added_cols"]
        assert "coupon" in changed["orders"]["removed_cols"]
        assert {"col": "region", "from": "varchar", "to": "text"} in changed["customers"]["type_changes"]
        # The localized title/body params agree with the diff itself.
        p = s["i18n"]
        assert p["agent"] == agent and p["connection"] == conn
        assert p["removed"] == len(diff["removed_tables"])
        assert p["modified"] == len(diff["changed"])
        assert p["total"] == diff["total"]
        # The seeded agent has no evals, so the loop will skip: no "View checks".
        assert s["reliability_check"] is None


def test_consecutive_schema_changes_are_kept_as_history(
    test_client, two_admins, no_reliability_loop, monkeypatch,
):
    org_id, token, _ = two_admins
    cid, ds_id = asyncio.run(_seed_agent(org_id, "Hist agent", "Hist conn"))

    rounds = [
        [table("orders", ("id", "int"), ("coupon", "text"), ("extra", "int")),
         table("customers", ("id", "int"), ("region", "varchar")), table("legacy", ("id", "int"))],
        [table("orders", ("id", "int"), ("coupon", "text"), ("extra", "int")),
         table("customers", ("id", "int"), ("region", "varchar"))],
    ]
    first_total = None
    for schema in rounds:
        async def discover(self, _s=schema, **kwargs):
            return _s
        monkeypatch.setattr(SqliteClient, "aget_schemas", discover)
        asyncio.run(_refresh(cid))
        [n] = _for(_inbox(test_client, token, org_id), type="schema_changed", agent_id=ds_id)
        first_total = first_total if first_total is not None else n["subject"]["diff"]["total"]

    s = n["subject"]
    # The latest diff is the second change only…
    assert s["diff"]["removed_tables"] == ["legacy"] and s["diff"]["changed"] == []
    # …and the first one was kept, not overwritten.
    assert len(s["history"]) == 1
    assert s["history"][0]["total"] == first_total
    assert s["history"][0]["at"]


def test_global_instruction_suggestion_links_to_the_instruction(test_client, two_admins):
    org_id, token, _ = two_admins
    from app.services.review_service import review_service
    ins_id = str(uuid.uuid4())

    async def emit():
        async with async_session_maker() as db:
            await review_service.emit(
                db, organization_id=org_id, type=TYPE_INSTRUCTION_SUGGESTION,
                data_source_id=None, title="Instruction change awaiting review",
                group_key=f"instr:{ins_id}", subject={"kind": "instruction", "instruction_id": ins_id},
            )
    asyncio.run(emit())

    [n] = [x for x in _for(_inbox(test_client, token, org_id), type="instruction_suggestion")
           if x["subject"].get("instruction_id") == ins_id]
    assert n["link"] == f"/agents/instructions/{ins_id}"
    assert n["subject"]["review_link"].startswith("/agents/review/")


async def _emit_low_conf(org_id, ds_id, n=3):
    from app.services.review_service import review_service
    async with async_session_maker() as db:
        return await review_service.emit(
            db, organization_id=org_id, type=TYPE_LOW_CONFIDENCE, data_source_id=ds_id,
            severity=SEVERITY_WARNING, title="Low-confidence answers",
            group_key=f"low_confidence:{ds_id}", subject={"kind": "low_confidence", "i18n": {"n": n}},
        )


async def _emit_suggestion(org_id, ds_id, ins_id):
    from app.services.review_service import review_service
    async with async_session_maker() as db:
        return await review_service.emit(
            db, organization_id=org_id, type=TYPE_INSTRUCTION_SUGGESTION, data_source_id=ds_id,
            title="Instruction change awaiting review", group_key=f"instr:{ins_id}",
            subject={"kind": "instruction", "instruction_id": ins_id},
        )


@pytest.mark.parametrize("path", ["resolve_open_for", "resolve_for_instruction"])
def test_resolving_the_item_resolves_every_recipients_notification(test_client, two_admins, path):
    org_id, token, token2 = two_admins
    from app.services.review_service import review_service
    _, ds_id = asyncio.run(_seed_agent(org_id, "Res agent", "Res conn"))
    ins_id = str(uuid.uuid4())

    async def emit_and_resolve():
        if path == "resolve_open_for":
            await _emit_low_conf(org_id, ds_id)
            async with async_session_maker() as db:
                assert await review_service.resolve_open_for(
                    db, organization_id=org_id, type=TYPE_LOW_CONFIDENCE,
                    data_source_id=ds_id, group_key=f"low_confidence:{ds_id}") == 1
        else:
            await _emit_suggestion(org_id, ds_id, ins_id)
            async with async_session_maker() as db:
                assert await review_service.resolve_for_instruction(
                    db, organization_id=org_id, instruction_id=ins_id) == 1

    ntype = "low_confidence" if path == "resolve_open_for" else "instruction_suggestion"
    expected_how = "condition_cleared" if path == "resolve_open_for" else "suggestion_reviewed"
    before = test_client.get("/api/notifications/count", headers=_h(token2, org_id)).json()["unread"]
    asyncio.run(emit_and_resolve())

    for tok in (token, token2):
        [n] = _for(_inbox(test_client, tok, org_id), type=ntype, agent_id=ds_id)
        assert n["resolved"] is True
        assert n["resolution"] == "resolved"
        assert n["read"] is True
        # The inbox says how it was handled.
        assert n["subject"]["handled"]["how"] == expected_how
    # A resolved item no longer counts toward the bell.
    assert test_client.get("/api/notifications/count", headers=_h(token2, org_id)).json()["unread"] == before


def test_dismissing_in_the_review_feed_closes_it_for_other_recipients(test_client, two_admins):
    org_id, token, token2 = two_admins
    _, ds_id = asyncio.run(_seed_agent(org_id, "Dis agent", "Dis conn"))
    item = asyncio.run(_emit_low_conf(org_id, ds_id))

    r = test_client.post(f"/api/review/{item.id}/dismiss", headers=_h(token, org_id))
    assert r.status_code == 200, r.text

    [n] = _for(_inbox(test_client, token2, org_id), type="low_confidence", agent_id=ds_id)
    assert n["resolved"] is True and n["resolution"] == "dismissed" and n["read"] is True
    # The other manager sees who handled it.
    me = test_client.get("/api/users/whoami", headers=_h(token, org_id)).json()
    assert n["subject"]["handled"]["how"] == "marked"
    assert n["subject"]["handled"]["by_user_id"] == me["id"]
    assert n["subject"]["handled"]["by_name"]

    # The Review feed's "Handled" view lists it with the same who/how.
    r = test_client.get("/api/review", params={"status": "resolved,dismissed", "agent_id": ds_id}, headers=_h(token2, org_id))
    assert r.status_code == 200, r.text
    [it] = [i for i in r.json()["items"] if i["id"] == str(item.id)]
    assert it["status"] == "dismissed"
    assert it["handled"]["how"] == "marked" and it["handled"]["by_user_id"] == me["id"] and it["handled"]["by_name"]


def test_a_recurring_condition_reopens_the_notification(test_client, two_admins):
    org_id, token, _ = two_admins
    from app.services.review_service import review_service
    _, ds_id = asyncio.run(_seed_agent(org_id, "Rec agent", "Rec conn"))

    async def cycle():
        await _emit_low_conf(org_id, ds_id, n=3)
        async with async_session_maker() as db:
            await review_service.resolve_open_for(
                db, organization_id=org_id, type=TYPE_LOW_CONFIDENCE,
                data_source_id=ds_id, group_key=f"low_confidence:{ds_id}")
        await _emit_low_conf(org_id, ds_id, n=5)
    asyncio.run(cycle())

    rows = _for(_inbox(test_client, token, org_id), type="low_confidence", agent_id=ds_id)
    assert len(rows) == 1
    assert rows[0]["resolved"] is False and rows[0]["resolution"] is None
    assert rows[0]["read"] is False
    assert rows[0]["subject"]["i18n"]["n"] == 5
    assert "handled" not in rows[0]["subject"]


def test_clear_handled_only_clears_the_callers_inbox(test_client, two_admins):
    org_id, token, token2 = two_admins
    from app.services.review_service import review_service
    _, ds_id = asyncio.run(_seed_agent(org_id, "Clr agent", "Clr conn"))

    async def handled_and_open():
        await _emit_low_conf(org_id, ds_id)
        async with async_session_maker() as db:
            await review_service.resolve_open_for(
                db, organization_id=org_id, type=TYPE_LOW_CONFIDENCE,
                data_source_id=ds_id, group_key=f"low_confidence:{ds_id}")
        await _emit_suggestion(org_id, ds_id, str(uuid.uuid4()))  # stays open
    asyncio.run(handled_and_open())

    r = test_client.post("/api/notifications/dismiss-resolved", headers=_h(token, org_id))
    assert r.status_code == 200 and r.json()["dismissed"] >= 1

    mine = _inbox(test_client, token, org_id)
    assert not _for(mine, type="low_confidence", agent_id=ds_id), "handled row is cleared for the caller"
    assert _for(mine, type="instruction_suggestion", agent_id=ds_id), "open rows are untouched"
    # The other manager still has it (greyed, handled).
    [other] = _for(_inbox(test_client, token2, org_id), type="low_confidence", agent_id=ds_id)
    assert other["resolved"] is True


# ── "Improve in training mode": the examples behind an item ──────────────────
# The feed's primary action opens a training conversation prefilled with what
# went wrong. /api/review/{id}/training-context returns those examples, read on
# demand from the agent's own conversations (never copied into the item).

async def _seed_runs(org_id, ds_id, user_id):
    """One conversation on the agent: a low-scored answer, a slow query and a
    failed query, plus an eval run's slow query that must not be surfaced."""
    from app.models.report import Report
    from app.models.completion import Completion
    from app.models.agent_execution import AgentExecution
    from app.models.tool_execution import ToolExecution
    from app.models.report_data_source_association import report_data_source_association as assoc
    async with async_session_maker() as db:
        rep = Report(title="r", slug=f"r-{uuid.uuid4().hex[:10]}", user_id=user_id, organization_id=org_id)
        db.add(rep)
        await db.flush()
        await db.execute(assoc.insert().values(report_id=rep.id, data_source_id=ds_id))
        # The judge stores the score on the head (user) completion.
        q = Completion(report_id=rep.id, role="user", prompt={"content": "Revenue by region last quarter?"},
                       completion={}, response_score=2, sigkill=datetime.utcnow())
        db.add(q)
        await db.flush()
        a = Completion(report_id=rep.id, role="system", parent_id=q.id, prompt={}, completion={"content": "…"},
                       response_score=None, sigkill=datetime.utcnow())
        db.add(a)
        await db.flush()
        for is_eval in (False, True):
            ae = AgentExecution(completion_id=a.id, report_id=rep.id, organization_id=org_id, is_eval_run=is_eval)
            db.add(ae)
            await db.flush()
            db.add(ToolExecution(agent_execution_id=ae.id, tool_name="create_data", success=True, status="success",
                                 duration_ms=125_000, arguments_json={"user_prompt": "eval question" if is_eval else "All orders ever"}))
            if not is_eval:
                db.add(ToolExecution(agent_execution_id=ae.id, tool_name="create_data", success=False, status="error",
                                     duration_ms=900, error_message="column region does not exist",
                                     arguments_json={"user_prompt": "Churn by region"}))
                # run_query only carries ids: its question is the turn's own.
                db.add(ToolExecution(agent_execution_id=ae.id, tool_name="run_query", success=True, status="success",
                                     duration_ms=95_000, arguments_json={"query_id": str(uuid.uuid4())}))
                # Still running (success is False until it finishes): not a failure.
                db.add(ToolExecution(agent_execution_id=ae.id, tool_name="create_data", success=False,
                                     status="in_progress", duration_ms=None,
                                     arguments_json={"user_prompt": "Still running"}))
        await db.commit()


async def _emit(org_id, ds_id, type_):
    from app.services.review_service import review_service
    async with async_session_maker() as db:
        it = await review_service.emit(db, organization_id=org_id, type=type_, data_source_id=ds_id,
                                       severity=SEVERITY_WARNING, title=type_, group_key=f"{type_}:{ds_id}",
                                       subject={"kind": type_})
        return str(it.id)


def test_training_context_returns_the_examples_behind_an_item(test_client, two_admins, whoami):
    from app.models.review_item import TYPE_SLOW_QUERY, TYPE_QUERY_ERROR
    org_id, token, _ = two_admins
    _, ds_id = asyncio.run(_seed_agent(org_id, "Sales agent", "Warehouse"))
    asyncio.run(_seed_runs(org_id, ds_id, whoami(token)["id"]))

    def ctx(type_):
        item_id = asyncio.run(_emit(org_id, ds_id, type_))
        items = test_client.get("/api/review", headers=_h(token, org_id)).json()["items"]
        acts = next(i for i in items if i["id"] == item_id)["actions"]
        assert acts[0]["id"] == "train_in_chat" and not acts[0].get("primary"), acts
        assert not any(a["id"] == "run_training" for a in acts)
        r = test_client.get(f"/api/review/{item_id}/training-context", headers=_h(token, org_id))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["agent_id"] == ds_id and body["type"] == type_
        return body["examples"]

    low = ctx(TYPE_LOW_CONFIDENCE)
    assert [e["question"] for e in low] == ["Revenue by region last quarter?"]
    assert low[0]["score"] == 2

    slow = ctx(TYPE_SLOW_QUERY)
    assert [(e["question"], e["seconds"]) for e in slow] == [
        ("All orders ever", 125), ("Revenue by region last quarter?", 95),
    ], "eval runs are excluded; run_query falls back to the turn's question"

    err = ctx(TYPE_QUERY_ERROR)
    assert [(e["question"], e["error"]) for e in err] == [("Churn by region", "column region does not exist")], \
        "an in-progress run is not a failed query"


def test_training_context_is_hidden_from_users_who_cannot_see_the_item(test_client, two_admins, create_user, login_user):
    org_id, token, _ = two_admins
    _, ds_id = asyncio.run(_seed_agent(org_id, "Sales agent", "Warehouse"))
    item_id = asyncio.run(_emit(org_id, ds_id, TYPE_LOW_CONFIDENCE))
    email = f"revn3_{uuid.uuid4().hex[:8]}@test.com"
    r = test_client.post(f"/api/organizations/{org_id}/members",
                         json={"organization_id": org_id, "email": email, "role": "member"},
                         headers=_h(token, org_id))
    assert r.status_code == 200, r.json()
    create_user(email=email, password="Test1234!")
    member = login_user(email, "Test1234!")
    r = test_client.get(f"/api/review/{item_id}/training-context", headers=_h(member, org_id))
    assert r.status_code == 404
    assert r.json()["error_code"] == "review_item.not_found"


def test_a_new_schema_change_after_handled_opens_a_new_item(
    test_client, two_admins, no_reliability_loop, monkeypatch,
):
    """Marking a schema change as handled must not silence the next change."""
    org_id, token, _ = two_admins
    cid, ds_id = asyncio.run(_seed_agent(org_id, "Again agent", "Again conn"))
    base = [table("orders", ("id", "int"), ("coupon", "text")),
            table("customers", ("id", "int"), ("region", "varchar")), table("legacy", ("id", "int"))]

    def change(schema):
        async def discover(self, **kwargs):
            return schema
        monkeypatch.setattr(SqliteClient, "aget_schemas", discover)
        asyncio.run(_refresh(cid))

    change(base[:2])  # legacy dropped
    items = test_client.get("/api/review", params={"agent_id": ds_id}, headers=_h(token, org_id)).json()["items"]
    [first] = [i for i in items if i["type"] == "schema_changed"]
    assert test_client.post(f"/api/review/{first['id']}/dismiss", headers=_h(token, org_id)).json()["ok"]

    change([table("orders", ("id", "int")), base[1]])  # coupon dropped: a different change
    items = test_client.get("/api/review", params={"agent_id": ds_id}, headers=_h(token, org_id)).json()["items"]
    [second] = [i for i in items if i["type"] == "schema_changed"]
    assert second["id"] != first["id"] and second["status"] == "open"
    assert second["subject"]["diff"]["changed"][0]["removed_cols"] == ["coupon"]
    [n] = _for(_inbox(test_client, token, org_id), type="schema_changed", agent_id=ds_id)
    assert not n["read"] and not n["resolved"], "the inbox row re-opens for the new change"


def test_cleared_handled_row_does_not_silence_the_next_schema_change(
    test_client, two_admins, no_reliability_loop, monkeypatch,
):
    """Clear handled (or ✕) on the old change, then a new change: it notifies."""
    org_id, token, _ = two_admins
    cid, ds_id = asyncio.run(_seed_agent(org_id, "Clr2 agent", "Clr2 conn"))
    base = [table("orders", ("id", "int"), ("coupon", "text")),
            table("customers", ("id", "int"), ("region", "varchar")), table("legacy", ("id", "int"))]

    def change(schema):
        async def discover(self, **kwargs):
            return schema
        monkeypatch.setattr(SqliteClient, "aget_schemas", discover)
        asyncio.run(_refresh(cid))

    change(base[:2])
    [first] = [i for i in test_client.get("/api/review", params={"agent_id": ds_id}, headers=_h(token, org_id)).json()["items"]
               if i["type"] == "schema_changed"]
    assert test_client.post(f"/api/review/{first['id']}/dismiss", headers=_h(token, org_id)).json()["ok"]
    assert test_client.post("/api/notifications/dismiss-resolved", headers=_h(token, org_id)).status_code == 200
    assert not _for(_inbox(test_client, token, org_id), type="schema_changed", agent_id=ds_id)

    change([table("orders", ("id", "int")), base[1]])
    [n] = _for(_inbox(test_client, token, org_id), type="schema_changed", agent_id=ds_id)
    assert not n["read"] and not n["resolved"]
    assert n["subject"]["diff"]["changed"][0]["removed_cols"] == ["coupon"]


def test_a_personal_dismiss_does_not_hide_the_next_change_on_an_open_item(
    test_client, two_admins, no_reliability_loop, monkeypatch,
):
    """✕ on the notification while the item is still open, then another change
    bumps the same item: the recipient is told about it again."""
    org_id, token, _ = two_admins
    cid, ds_id = asyncio.run(_seed_agent(org_id, "Open agent", "Open conn"))
    base = [table("orders", ("id", "int"), ("coupon", "text")),
            table("customers", ("id", "int"), ("region", "varchar")), table("legacy", ("id", "int"))]

    def change(schema):
        async def discover(self, **kwargs):
            return schema
        monkeypatch.setattr(SqliteClient, "aget_schemas", discover)
        asyncio.run(_refresh(cid))

    change(base[:2])
    [n] = _for(_inbox(test_client, token, org_id), type="schema_changed", agent_id=ds_id)
    assert test_client.post(f"/api/notifications/{n['id']}/dismiss", headers=_h(token, org_id)).status_code == 200
    assert not _for(_inbox(test_client, token, org_id), type="schema_changed", agent_id=ds_id)

    change([table("orders", ("id", "int")), base[1]])
    [again] = _for(_inbox(test_client, token, org_id), type="schema_changed", agent_id=ds_id)
    assert not again["read"]
    assert again["subject"]["diff"]["changed"][0]["removed_cols"] == ["coupon"]
    assert len(again["subject"]["history"]) == 1, "same open item, previous change kept as history"


def test_view_checks_is_offered_only_when_a_check_will_run(
    test_client, two_admins, no_reliability_loop, monkeypatch,
):
    from app.services.agent_reliability_service import AgentReliabilityService

    async def will(self, db, organization, data_source, trigger):
        return True
    monkeypatch.setattr(AgentReliabilityService, "will_check", will)
    org_id, token, _ = two_admins
    cid, ds_id = asyncio.run(_seed_agent(org_id, "Checks agent", "Checks conn"))

    async def discover(self, **kwargs):
        return [table("orders", ("id", "int")), table("customers", ("id", "int"), ("region", "varchar"))]
    monkeypatch.setattr(SqliteClient, "aget_schemas", discover)
    asyncio.run(_refresh(cid))
    [n] = _for(_inbox(test_client, token, org_id), type="schema_changed", agent_id=ds_id)
    assert n["subject"]["reliability_check"]["started_at"]

"""Readers chat about a published dashboard (phase 7, part C).

`/api/d/{id}/chat` is the conversation's viewer chat, keyed on the
dashboard: one thread per reader per dashboard (reports.source_artifact_id),
talking about that dashboard's live release only, with the dashboard's chat
settings (sources and model; none chosen: the release's sources and the
organization's model), and -- with no agents -- given only the rows that
reader may see. It outlives the conversation. The conversation's own /r chat
is unchanged.

Sending a message runs the model, which the tests do not; they cover who
may chat, the thread, its model and roster, and the context handed to it.
"""
import asyncio

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

CONV = "conv_three_dash"


def _rows(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _chatty(corpus, corpus_rows, test_client, key, visibility="internal", **settings):
    aid = own(corpus_rows, corpus, key)
    publish(aid)
    assert share(test_client, corpus, aid, visibility).status_code == 200
    corpus_rows.update("artifacts", aid, chat_enabled=True, **settings)
    return aid


def _context(corpus, aid, person):
    """What a message from `person` would hand the model, and its thread."""
    from app.dependencies import async_session_maker
    from app.models.artifact import Artifact
    from app.models.organization import Organization
    from app.models.user import User
    from app.services import dashboard_chat
    from app.services.dashboard_access import published_release

    async def go():
        async with async_session_maker() as db:
            artifact = await db.get(Artifact, aid)
            release = await published_release(db, artifact)
            org = await db.get(Organization, corpus.cast.org_id)
            user = await db.get(User, corpus.cast.id(person))
            agents = await dashboard_chat.effective_agent_ids(db, artifact, release, org, user)
            thread = await dashboard_chat.resolve_thread(db, artifact, user, agents)
            context = await dashboard_chat.build_context(db, artifact, release, agents, user)
            return context, {"id": str(thread.id), "model_id": thread.model_id,
                             "source_artifact_id": thread.source_artifact_id,
                             "forked_from_id": thread.forked_from_id}

    return asyncio.run(go())


@pytest.mark.e2e
def test_who_may_chat(corpus, corpus_rows, test_client):
    aid = _chatty(corpus, corpus_rows, test_client, f"{CONV}/dash_sales", visibility="public")
    path = f"/api/d/{aid}/chat"
    ok = test_client.get(path, headers=as_(corpus, "member")).json()
    assert ok["available"] is True and ok["chat_report_id"] is None
    assert test_client.get(path, headers=as_(corpus, "anonymous")).json()["reason"] == "auth_required"
    assert test_client.get(path, headers=as_(corpus, "other_org")).json()["reason"] == "not_member"
    corpus_rows.update("artifacts", aid, chat_enabled=False)
    assert test_client.get(path, headers=as_(corpus, "member")).json()["reason"] == "disabled"
    hidden = own(corpus_rows, corpus, f"{CONV}/dash_stock")
    publish(hidden)
    corpus_rows.update("artifacts", hidden, chat_enabled=True)  # not shared
    assert test_client.get(f"/api/d/{hidden}/chat", headers=as_(corpus, "member")).status_code == 404
    for sent in (test_client.post(f"{path}/completions", json={"prompt": {"content": "hi"}},
                                  headers=as_(corpus, "member")),):
        assert sent.status_code == 403  # disabled: refused before anything runs


@pytest.mark.e2e
def test_each_dashboard_has_its_own_chat_about_itself(corpus, corpus_rows, test_client):
    sales = _chatty(corpus, corpus_rows, test_client, f"{CONV}/dash_sales", chat_data_source_ids=[])
    stock = _chatty(corpus, corpus_rows, test_client, f"{CONV}/dash_stock", chat_data_source_ids=[])
    sales_ctx, sales_thread = _context(corpus, sales, "member")
    stock_ctx, stock_thread = _context(corpus, stock, "member")
    assert sales_thread["id"] != stock_thread["id"]
    assert (sales_thread["source_artifact_id"], stock_thread["source_artifact_id"]) == (sales, stock)
    assert sales_thread["forked_from_id"] is None  # never mistaken for the conversation's /r chat
    assert sales_ctx["artifact_title"] == "לוח מכירות" and stock_ctx["artifact_title"] == "לוח מלאי"
    assert [v["title"] for v in sales_ctx["visualizations"]] != [v["title"] for v in stock_ctx["visualizations"]]
    assert sales_ctx["scope"] == "data_only"
    # The same reader gets the same thread back.
    assert _context(corpus, sales, "member")[1]["id"] == sales_thread["id"]
    status = test_client.get(f"/api/d/{sales}/chat", headers=as_(corpus, "member")).json()
    assert status["chat_report_id"] == sales_thread["id"]
    # The conversation's own chat does not see these threads.
    assert test_client.get(f"/api/r/{corpus.reports[CONV]}/chat",
                           headers=as_(corpus, "member")).json().get("chat_report_id") is None


@pytest.mark.e2e
def test_a_readers_chat_gets_only_the_rows_they_may_see(corpus, corpus_rows, test_client):
    aid = _chatty(corpus, corpus_rows, test_client, "identity_param/dash", chat_data_source_ids=[])
    theirs, _ = _context(corpus, aid, "member")
    assert theirs["visualizations"] and all(v["rows"] == [] for v in theirs["visualizations"])
    own_ctx, _ = _context(corpus, aid, "owner")
    assert all(v["rows"] for v in own_ctx["visualizations"])


@pytest.mark.e2e
def test_the_chat_follows_the_dashboards_settings(corpus, corpus_rows, test_client):
    aid = _chatty(corpus, corpus_rows, test_client, f"{CONV}/dash_sales")
    ctx, thread = _context(corpus, aid, "member")
    # None chosen: the release's sources, the organization's model.
    assert ctx["scope"] == "agents" and thread["model_id"] is None
    release_sources = {r["data_source_id"] for r in _rows(
        corpus_rows, "SELECT d.data_source_id FROM artifact_release_data_source_association d "
                     "JOIN artifacts a ON a.published_release_id = d.artifact_release_id WHERE a.id = :a", a=aid)}
    roster = {r["data_source_id"] for r in _rows(
        corpus_rows, "SELECT data_source_id FROM report_data_source_association WHERE report_id = :r",
        r=thread["id"])}
    assert roster == release_sources
    corpus_rows.update("artifacts", aid, chat_model_id="org_default", chat_data_source_ids=[])
    ctx, thread = _context(corpus, aid, "member")
    assert ctx["scope"] == "data_only" and thread["model_id"] is None
    assert _rows(corpus_rows, "SELECT data_source_id FROM report_data_source_association WHERE report_id = :r",
                 r=thread["id"]) == []


@pytest.mark.e2e
def test_the_chat_outlives_the_conversation(corpus, corpus_rows, test_client):
    aid = _chatty(corpus, corpus_rows, test_client, f"{CONV}/dash_sales")
    assert test_client.delete(f"/api/reports/{corpus.reports[CONV]}", headers=as_(corpus, "owner")).status_code == 200
    status = test_client.get(f"/api/d/{aid}/chat", headers=as_(corpus, "member")).json()
    assert status["available"] is True
    ctx, _ = _context(corpus, aid, "member")
    assert ctx["artifact_title"] == "לוח מכירות"


# ── review fixes ─────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_an_auto_dashboards_chat_never_widens_to_the_whole_organization(corpus, corpus_rows, test_client):
    """No sources of its own and code that names none: data only, not every
    agent the reader can reach."""
    aid = _chatty(corpus, corpus_rows, test_client, "auto_conv/dash")
    ctx, thread = _context(corpus, aid, "member")
    assert ctx["scope"] == "data_only"
    assert _rows(corpus_rows, "SELECT data_source_id FROM report_data_source_association WHERE report_id = :r",
                 r=thread["id"]) == []


@pytest.mark.e2e
def test_a_reader_the_dashboard_is_not_shared_with_is_refused_not_told_to_join(corpus, corpus_rows, test_client):
    aid = _chatty(corpus, corpus_rows, test_client, f"{CONV}/dash_sales", visibility="shared")
    assert share(test_client, corpus, aid, "shared", users=["direct_share"]).status_code == 200
    assert test_client.get(f"/api/d/{aid}/chat", headers=as_(corpus, "member")).status_code == 403
    assert test_client.get(f"/api/d/{aid}/chat", headers=as_(corpus, "direct_share")).json()["available"] is True


@pytest.mark.e2e
def test_messages_and_threads_stay_with_their_reader(corpus, corpus_rows, test_client):
    aid = _chatty(corpus, corpus_rows, test_client, f"{CONV}/dash_sales", visibility="public")
    path = f"/api/d/{aid}/chat/completions"
    assert test_client.post(path, json={"prompt": {"content": "hi"}},
                            headers=as_(corpus, "anonymous")).status_code == 401
    assert test_client.post(path, json={"prompt": {"content": "hi"}},
                            headers=as_(corpus, "other_org")).status_code == 403
    _, mine = _context(corpus, aid, "member")
    theirs = test_client.get(path, headers=as_(corpus, "admin")).json()
    assert theirs["report_id"] is None  # the admin's own (none yet), never the member's
    assert test_client.get(f"{path}/abc/stream", headers=as_(corpus, "admin")).status_code == 404


# ── host context (finding 2) ─────────────────────────────────────────────

def _capture_completion(monkeypatch) -> list:
    """Stop at the completion service (the model is not run) and keep what
    the route handed it."""
    from app.services.completion_service import CompletionService

    seen: list = []

    async def fake_stream(self, db, report_id, completion, user, organization, *args, **kwargs):
        seen.append({"report_id": report_id, "prompt": completion.prompt.model_copy(deep=True)})
        return {"captured": True}

    monkeypatch.setattr(CompletionService, "create_completion_stream", fake_stream)
    return seen


@pytest.mark.e2e
def test_the_bubble_hands_the_hosts_context_to_the_message_as_advisory_context(
        corpus, corpus_rows, test_client, monkeypatch):
    """Inside a host application the bubble sends the host's current context.
    It reaches the message's platform context, small JSON objects only, and
    never stays on the prompt itself."""
    aid = _chatty(corpus, corpus_rows, test_client, f"{CONV}/dash_sales", chat_data_source_ids=[])
    path = f"/api/d/{aid}/chat/completions"
    seen = _capture_completion(monkeypatch)
    hc = {"customerId": "C-14", "region": "EMEA"}

    sent = test_client.post(path, json={"prompt": {"content": "what did this customer buy?", "platform_context": {"host_context": hc}}},
                            headers=as_(corpus, "member"))
    assert sent.status_code == 200 and sent.json() == {"captured": True}
    prompt = seen[-1]["prompt"]
    assert prompt.platform == "artifact_chat"
    assert prompt.platform_context["host_context"] == hc
    assert prompt.platform_context["artifact_title"] == "לוח מכירות"  # the server's own context is still there

    for dropped in ({"k": "x" * 3000}, {f"k{i}": i for i in range(21)}):
        assert test_client.post(path, json={"prompt": {"content": "and now?", "platform_context": {"host_context": dropped}}},
                                headers=as_(corpus, "member")).status_code == 200
        prompt = seen[-1]["prompt"]
        assert "host_context" not in prompt.platform_context
    for not_an_object in ("C-14", ["C-14"]):  # ignored by the context sanitizer
        assert test_client.post(path, json={"prompt": {"content": "and now?", "platform_context": {"host_context": not_an_object}}},
                                headers=as_(corpus, "member")).status_code == 200
        assert "host_context" not in seen[-1]["prompt"].platform_context

    assert test_client.post(path, json={"prompt": {"content": "no host"}},
                            headers=as_(corpus, "member")).status_code == 200
    assert "host_context" not in seen[-1]["prompt"].platform_context


@pytest.mark.e2e
def test_host_context_cannot_widen_what_a_reader_may_see(corpus, corpus_rows, test_client, monkeypatch):
    """Host context is prompt text only: naming another user, a role, or the
    owner does not change the rows the reader's message is given."""
    aid = _chatty(corpus, corpus_rows, test_client, "identity_param/dash", chat_data_source_ids=[])
    seen = _capture_completion(monkeypatch)
    owner_id = corpus.cast.id("owner")
    hc = {"user_id": owner_id, "role": "admin", "as": "owner", "customer_id": "C-14"}
    assert test_client.post(f"/api/d/{aid}/chat/completions",
                            json={"prompt": {"content": "show me everything", "platform_context": {"host_context": hc}}},
                            headers=as_(corpus, "member")).status_code == 200
    ctx = seen[-1]["prompt"].platform_context
    assert ctx["host_context"] == hc
    without = dict(ctx)
    without.pop("host_context")
    theirs, _ = _context(corpus, aid, "member")
    assert without == theirs  # exactly the reader's own context, rows withheld as before
    assert ctx["visualizations"] and all(v["rows"] == [] for v in ctx["visualizations"])


@pytest.mark.e2e
def test_the_conversations_own_chat_drops_host_context(corpus, corpus_rows, test_client, monkeypatch):
    rid = corpus.reports[CONV]
    assert test_client.put(f"/api/reports/{rid}/visibility/artifact",
                           json={"visibility": "internal", "artifact_chat_enabled": True},
                           headers=as_(corpus, "owner")).status_code == 200
    seen = _capture_completion(monkeypatch)
    assert test_client.post(f"/api/r/{rid}/chat/completions",
                            json={"prompt": {"content": "hi", "platform_context": {"host_context": {"customerId": "C-14"}}}},
                            headers=as_(corpus, "member")).status_code == 200
    prompt = seen[-1]["prompt"]
    assert "host_context" not in (prompt.platform_context or {})

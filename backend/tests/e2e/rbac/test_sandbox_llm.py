"""`llm` in generated code: column-level LLM functions (classify / filter /
score / extract / generate) that run on the trusted side.

Contract:
- the helper exists only when the org turns on "LLM functions in code";
- calls take a whole column and return one answer per row, aligned to the
  input's index; answers that break the requested shape become None;
- repeated values are sent once, values are batched into few provider
  requests, and a value answered before (same model, same question) is not
  sent again;
- the model is named by its record id (LLMModel.id) or, as a shorthand, by a
  provider model id that matches exactly one record; the user must be allowed
  to use it (restricted models), on the agent's run;
- classify / filter / score go to the org's decision model when there is one
  the user may use, unless the code names a chat model; extract / generate
  always use a chat model;
- every provider request is recorded as LLM usage, attributed to the org,
  user and report, under the record that served it;
- a per-run cap on values sent stops the run before any request, with a
  message the coder can act on.

Boundaries faked: the Anthropic client (chat) and the decision HTTP server.
Everything else - DB, access rules, the sandbox child, the RPC - runs real.
"""
from __future__ import annotations

import asyncio
import json
import re
import types
import uuid

import pytest

from app.dependencies import async_session_maker
from tests.mocks.fake_decision_server import FakeDecisionServer


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


LICENSE_FEATURES = ["llm_access_control", "custom_roles", "audit_logs"]


@pytest.fixture
def access_license(enterprise_license, monkeypatch):
    from app.ee import license as ee_license
    info = ee_license.LicenseInfo(
        licensed=True, tier="enterprise", org_name="sandbox-llm", features=LICENSE_FEATURES,
        license_id="test-sandbox-llm",
    )
    monkeypatch.setattr(ee_license, "get_license_info", lambda force_refresh=False: info)
    saved = (ee_license._cached_license, ee_license._cache_initialized)
    ee_license._cached_license = info
    ee_license._cache_initialized = True
    try:
        yield
    finally:
        ee_license._cached_license, ee_license._cache_initialized = saved


# ── provider boundaries ──────────────────────────────────────────────────────

class FakeChat:
    """Stands in for the Anthropic API. Reads the items the app sent, answers
    each with `answer(value, prompt)`, and records every request."""

    def __init__(self):
        self.requests: list[dict] = []
        self.answer = lambda value, prompt: "positive"

    def items_sent(self) -> list[str]:
        return [it["v"] for r in self.requests for it in r["items"]]

    def __call__(self, client_self, model_id, prompt, images=None, system=None, thinking=None):
        from app.ai.llm.types import LLMResponse, LLMUsage
        m = re.search(r"ITEMS:\s*(\[.*\])\s*$", prompt, re.S)
        items = json.loads(m.group(1)) if m else []
        self.requests.append({"model_id": model_id, "prompt": prompt, "system": system, "items": items})
        answers = {str(it["i"]): self.answer(it["v"], prompt) for it in items}
        return LLMResponse(text=json.dumps({"answers": answers}),
                           usage=LLMUsage(prompt_tokens=100 + 10 * len(items), completion_tokens=5 * len(items)))


@pytest.fixture
def fake_chat(monkeypatch):
    from app.ai.llm.clients import anthropic_client
    chat = FakeChat()

    def _inference(self, model_id, prompt, images=None, system=None, thinking=None):
        return chat(self, model_id, prompt, images=images, system=system, thinking=thinking)

    monkeypatch.setattr(anthropic_client.Anthropic, "inference", _inference)
    return chat


@pytest.fixture
def fake_decisions(monkeypatch):
    from app.ai.llm import decisions
    server = FakeDecisionServer()
    monkeypatch.setattr(decisions, "TRANSPORT_OVERRIDE", server.transport())
    return server


@pytest.fixture(autouse=True)
def _fresh_answer_cache():
    from app.ai.code_execution import sandbox_llm
    sandbox_llm.ANSWER_CACHE.clear()
    yield
    sandbox_llm.ANSWER_CACHE.clear()


@pytest.fixture(params=["subprocess", "inprocess"])
def sandbox_mode(request, monkeypatch):
    monkeypatch.setenv("BOW_CODE_SANDBOX", request.param)
    return request.param


# ── world ────────────────────────────────────────────────────────────────────

def _models(client, t, org, kind=None):
    r = client.get("/api/llm/models", params={"kind": kind} if kind else {}, headers=_h(t, org))
    assert r.status_code == 200, r.json()
    return r.json()


def _set(client, t, org, **values):
    r = client.put("/api/organization/settings", json={"config": {k: {"value": v} for k, v in values.items()}},
                   headers=_h(t, org))
    assert r.status_code == 200, r.json()


@pytest.fixture
def world(test_client, bootstrap_admin, invite_user_to_org, create_group, add_user_to_group,
          grant_resource, access_license):
    """Two Anthropic providers (both serve `shared_mid`, so that id is
    ambiguous), a restricted chat model granted to the Finance group only."""
    admin = bootstrap_admin("sbllm")
    t, org = admin["token"], admin["org_id"]
    sfx = uuid.uuid4().hex[:6]
    shared_mid = f"claude-shared-{sfx}"
    r = test_client.post("/api/llm/providers", json={
        "name": f"anth-a-{sfx}", "provider_type": "anthropic", "credentials": {"api_key": "dummy-key"},
        "models": [
            {"model_id": f"claude-small-{sfx}", "name": f"Small {sfx}", "is_custom": True},
            {"model_id": shared_mid, "name": f"Shared A {sfx}", "is_custom": True},
            {"model_id": f"claude-secret-{sfx}", "name": f"Secret {sfx}", "is_custom": True},
        ],
    }, headers=_h(t, org))
    assert r.status_code == 200, r.json()
    r = test_client.post("/api/llm/providers", json={
        "name": f"anth-b-{sfx}", "provider_type": "anthropic", "credentials": {"api_key": "dummy-key"},
        "models": [{"model_id": shared_mid, "name": f"Shared B {sfx}", "is_custom": True}],
    }, headers=_h(t, org))
    assert r.status_code == 200, r.json()
    chat = {m["model_id"] + "|" + m["name"]: m for m in _models(test_client, t, org)}
    small = next(m for k, m in chat.items() if k.startswith(f"claude-small-{sfx}|"))
    secret = next(m for k, m in chat.items() if k.startswith(f"claude-secret-{sfx}|"))
    shared = [m for k, m in chat.items() if k.startswith(shared_mid + "|")]
    assert len(shared) == 2
    assert test_client.post(f"/api/llm/models/{small['id']}/set_default?small=true",
                            headers=_h(t, org)).status_code == 200

    finance = create_group(name=f"Finance-{sfx}", user_token=t, org_id=org).json()["id"]
    fiona = invite_user_to_org(org_id=org, admin_token=t)
    mark = invite_user_to_org(org_id=org, admin_token=t)
    assert add_user_to_group(group_id=finance, user_id=fiona["user_id"], user_token=t,
                             org_id=org).status_code in (200, 201)
    assert test_client.put(f"/api/llm/models/{secret['id']}/restricted", json={"is_restricted": True},
                           headers=_h(t, org)).status_code == 200
    g = grant_resource(resource_type="llm_model", resource_id=secret["id"], principal_type="group",
                       principal_id=finance, permissions=["use"], user_token=t, org_id=org)
    assert g.status_code in (200, 201), g.json()

    _set(test_client, t, org, enable_sandbox_llm=True)
    return types.SimpleNamespace(
        client=test_client, t=t, org=org, admin=admin, fiona=fiona, mark=mark,
        small=small, secret=secret, shared=shared, shared_mid=shared_mid, sfx=sfx,
    )


def _add_decision_model(w) -> dict:
    r = w.client.post("/api/llm/providers", json={
        "name": f"decide-{w.sfx}", "provider_type": "custom",
        "credentials": {"base_url": "https://decisions.example.com/v1", "api_key": "dummy-key"},
        "models": [{"model_id": "jev-1.14.0", "name": f"Decider {w.sfx}", "is_custom": True,
                    "model_kind": "decision", "config": {"decision_format": "systemone"}}],
    }, headers=_h(w.t, w.org))
    assert r.status_code == 200, r.json()
    return _models(w.client, w.t, w.org, kind="decision")[0]


# ── running code the way the app does ───────────────────────────────────────

def _user_id(u) -> str:
    return str(u.get("user_id") or u.get("id") or u["user"]["id"])


async def _build(org_id, user_id, *, rerun=False, report_id=None, step_id=None):
    from app.ai.code_execution.sandbox_helpers import LLMScope, build_sandbox_helpers
    from app.models.organization import Organization

    async with async_session_maker() as db:
        org = await db.get(Organization, org_id)
        settings = await org.get_settings(db)
    helpers = build_sandbox_helpers(
        settings, [], rerun=rerun,
        llm_scope=LLMScope(organization_id=str(org_id), user_id=user_id, report_id=report_id, step_id=step_id),
    )
    return settings, helpers


def run_code(org_id, user_id, code, *, rerun=False, report_id=None, step_id=None):
    """Build the helpers on an event loop and execute the code off it, as the
    agent's create_data and every re-run do. Returns (df, log, helpers)."""
    from app.ai.code_execution.code_execution import StreamingCodeExecutor

    async def _go():
        settings, helpers = await _build(org_id, user_id, rerun=rerun, report_id=report_id, step_id=step_id)
        executor = StreamingCodeExecutor(organization_settings=settings, sandbox_helpers=helpers)
        df, log, _ = await asyncio.to_thread(
            lambda: executor.execute_code(code=code, ds_clients={}, excel_files=[]))
        # Usage rows are written after each call, in the background.
        if helpers is not None and helpers.llm is not None:
            await helpers.llm.drain()
        return df, log, helpers

    return asyncio.run(_go())


def prompt_for(org_id, user_id) -> str:
    """The coder prompt text that documents `llm` for this user."""
    from app.ai.tools.implementations.create_data import sandbox_llm_directive

    async def _go():
        _, helpers = await _build(org_id, user_id)
        return await sandbox_llm_directive(helpers.llm)

    return asyncio.run(_go())


COMMENTS = ["love it", "terrible", "meh", "love it", "great value", "terrible", "broken on arrival"]


def _sentiment(value, _prompt):
    return {"love it": "positive", "great value": "positive", "terrible": "negative",
            "broken on arrival": "negative", "meh": "neutral"}.get(value, "neutral")


# ── tests ────────────────────────────────────────────────────────────────────

def test_classify_answers_every_row_with_an_allowed_label_aligned_to_the_index(world, fake_chat, sandbox_mode):
    # One value gets an answer outside the label set: it must come back None,
    # never as a label the caller did not ask for.
    fake_chat.answer = lambda v, p: "furious" if v == "meh" else _sentiment(v, p)
    code = f'''
def generate_df(ds_clients, excel_files, llm):
    df = pd.DataFrame({{"comment": {COMMENTS!r}}}, index=[10 + i for i in range({len(COMMENTS)})])
    df["sentiment"] = llm.classify(df["comment"], ["positive", "negative", "neutral"])
    return df
'''
    df, _, _ = run_code(world.org, _user_id(world.admin), code)
    assert list(df.index) == [10 + i for i in range(len(COMMENTS))]
    expected = [None if c == "meh" else _sentiment(c, "") for c in COMMENTS]
    assert [None if v is None or v != v else v for v in df["sentiment"]] == expected


def test_repeated_values_are_sent_once_in_few_requests(world, fake_chat, sandbox_mode):
    fake_chat.answer = _sentiment
    code = f'''
def generate_df(ds_clients, excel_files, llm):
    df = pd.DataFrame({{"comment": {COMMENTS!r} * 300}})
    df["sentiment"] = llm.classify(df["comment"], ["positive", "negative", "neutral"])
    return df
'''
    df, _, _ = run_code(world.org, _user_id(world.admin), code)
    assert len(df) == len(COMMENTS) * 300 and df["sentiment"].notna().all()
    sent = fake_chat.items_sent()
    assert sorted(sent) == sorted(set(COMMENTS))  # each distinct value exactly once
    assert len(fake_chat.requests) == 1


def test_a_rerun_with_values_answered_before_makes_no_provider_request(world, fake_chat):
    fake_chat.answer = _sentiment
    code = f'''
def generate_df(ds_clients, excel_files, llm):
    df = pd.DataFrame({{"comment": {COMMENTS!r}}})
    df["sentiment"] = llm.classify(df["comment"], ["positive", "negative", "neutral"])
    return df
'''
    first, _, _ = run_code(world.org, _user_id(world.admin), code)
    calls = len(fake_chat.requests)
    assert calls >= 1
    again, _, _ = run_code(world.org, _user_id(world.admin), code, rerun=True)
    assert len(fake_chat.requests) == calls
    assert list(again["sentiment"]) == list(first["sentiment"])
    # A different question about the same values is a different answer.
    other = code.replace('["positive", "negative", "neutral"]', '["positive", "negative"]')
    run_code(world.org, _user_id(world.admin), other)
    assert len(fake_chat.requests) > calls


def test_a_model_is_named_by_record_id_or_by_a_model_id_that_matches_one_record(world, fake_chat):
    fake_chat.answer = _sentiment
    tmpl = '''
def generate_df(ds_clients, excel_files, llm):
    out = llm.classify(["love it"], ["positive", "negative"], model=%r)
    return pd.DataFrame({"label": out})
'''
    uid = _user_id(world.admin)
    # By record id: the request goes to that record's provider model.
    run_code(world.org, uid, tmpl % world.shared[1]["id"])
    assert fake_chat.requests[-1]["model_id"] == world.shared_mid
    # By a unique provider model id.
    run_code(world.org, uid, tmpl % world.small["model_id"])
    assert fake_chat.requests[-1]["model_id"] == world.small["model_id"]
    # An id two records share is refused, naming both records to pick from.
    n = len(fake_chat.requests)
    with pytest.raises(Exception) as ei:
        run_code(world.org, uid, tmpl % world.shared_mid)
    assert all(m["id"] in str(ei.value) for m in world.shared)
    # An unknown model lists what the user can use.
    with pytest.raises(Exception) as ei:
        run_code(world.org, uid, tmpl % "no-such-model")
    assert world.small["id"] in str(ei.value)
    assert len(fake_chat.requests) == n


def test_a_restricted_model_works_only_for_users_allowed_to_use_it(world, fake_chat):
    fake_chat.answer = _sentiment
    code = '''
def generate_df(ds_clients, excel_files, llm):
    return pd.DataFrame({"label": llm.classify(["love it"], ["positive", "negative"], model=%r)})
''' % world.secret["id"]
    df, _, _ = run_code(world.org, _user_id(world.fiona), code)
    assert list(df["label"]) == ["positive"]
    n = len(fake_chat.requests)
    with pytest.raises(Exception) as ei:
        run_code(world.org, _user_id(world.mark), code)
    assert world.secret["id"] in str(ei.value)
    assert len(fake_chat.requests) == n  # refused before any request


def test_the_coder_prompt_lists_model_id_and_record_id_of_usable_models_only(world, fake_chat):
    fiona = prompt_for(world.org, _user_id(world.fiona))
    mark = prompt_for(world.org, _user_id(world.mark))
    for text in (fiona, mark):
        assert world.small["id"] in text and world.small["model_id"] in text
        assert "llm.classify" in text
    assert world.secret["id"] in fiona and world.secret["model_id"] in fiona
    assert world.secret["id"] not in mark and world.secret["model_id"] not in mark


def test_classify_filter_score_use_the_decision_model_and_return_probabilities(world, fake_chat, fake_decisions):
    dm = _add_decision_model(world)
    fake_decisions.pick("negative", confidence=0.83)
    code = '''
def generate_df(ds_clients, excel_files, llm):
    df = pd.DataFrame({"comment": ["terrible", "terrible", "awful service"]})
    p = llm.classify(df["comment"], ["positive", "negative"], with_proba=True)
    df["label"], df["p"] = p["label"], p["p"]
    df["refund"] = llm.filter(df["comment"], "asks for a refund")
    df["urgency"] = llm.score(df["comment"], "how urgent", low=1, high=5)
    return df
'''
    df, _, _ = run_code(world.org, _user_id(world.admin), code)
    assert list(df["label"]) == ["negative"] * 3
    assert all(abs(float(p) - 0.83) < 1e-6 for p in df["p"])
    assert df["refund"].dtype == bool and bool(df["refund"].all())  # p=0.83 >= 0.5
    assert all(1 <= float(u) <= 5 for u in df["urgency"])
    assert fake_chat.requests == []  # nothing went to a chat model
    # One request per distinct value per call, never per row.
    assert fake_decisions.calls == 2 * 3
    assert all(r["json"]["model"] == dm["model_id"] for r in fake_decisions.requests)


def test_naming_a_chat_model_overrides_the_decision_model(world, fake_chat, fake_decisions):
    _add_decision_model(world)
    fake_chat.answer = _sentiment
    code = '''
def generate_df(ds_clients, excel_files, llm):
    return pd.DataFrame({"label": llm.classify(["love it"], ["positive", "negative"], model=%r)})
''' % world.small["id"]
    df, _, _ = run_code(world.org, _user_id(world.admin), code)
    assert list(df["label"]) == ["positive"]
    assert fake_decisions.calls == 0 and len(fake_chat.requests) == 1


def test_extract_and_generate_use_a_chat_model_even_with_a_decision_model(world, fake_chat, fake_decisions):
    _add_decision_model(world)

    def answer(value, prompt):
        if "FIELDS" in prompt:
            return {"product": value.split()[0], "qty": len(value.split())}
        return value.upper()

    fake_chat.answer = answer
    code = '''
def generate_df(ds_clients, excel_files, llm):
    s = pd.Series(["widget broke twice", "gizmo late"])
    out = llm.extract(s, {"product": "str", "qty": "int"})
    out["shout"] = llm.generate(s, "repeat the text in capitals")
    return out
'''
    df, _, _ = run_code(world.org, _user_id(world.admin), code)
    assert list(df["product"]) == ["widget", "gizmo"]
    assert [int(q) for q in df["qty"]] == [3, 2]
    assert list(df["shout"]) == ["WIDGET BROKE TWICE", "GIZMO LATE"]
    assert fake_decisions.calls == 0


def test_every_provider_request_is_recorded_as_usage_on_the_report(world, fake_chat, create_report):
    from sqlalchemy import select
    from app.models.llm_usage_record import LLMUsageRecord

    fake_chat.answer = _sentiment
    report = create_report(title="llm usage", user_token=world.t, org_id=world.org)
    code = f'''
def generate_df(ds_clients, excel_files, llm):
    return pd.DataFrame({{"label": llm.classify({COMMENTS!r}, ["positive", "negative", "neutral"])}})
'''
    run_code(world.org, _user_id(world.admin), code, report_id=report["id"], step_id="step-x")

    async def _rows():
        async with async_session_maker() as db:
            res = await db.execute(select(LLMUsageRecord).where(LLMUsageRecord.scope == "sandbox_llm"))
            return list(res.scalars().all())

    rows = asyncio.run(_rows())
    assert len(rows) == len(fake_chat.requests) >= 1
    for r in rows:
        assert str(r.organization_id) == str(world.org)
        assert str(r.user_id) == _user_id(world.admin)
        assert str(r.report_id) == str(report["id"])
        assert r.scope_ref_id == "step-x"
        assert str(r.llm_model_id) == world.small["id"]
        assert r.prompt_tokens > 0


def test_the_run_summary_reports_what_was_sent_and_what_came_from_cache(world, fake_chat):
    fake_chat.answer = _sentiment
    code = f'''
def generate_df(ds_clients, excel_files, llm):
    return pd.DataFrame({{"label": llm.classify({COMMENTS!r}, ["positive", "negative", "neutral"])}})
'''
    _, log, helpers = run_code(world.org, _user_id(world.admin), code)
    s = helpers.summary()["llm"]
    assert s["rows"] == len(COMMENTS) and s["unique_values"] == len(set(COMMENTS))
    assert s["values_sent"] == len(set(COMMENTS)) and s["cache_hits"] == 0
    assert s["requests"] == len(fake_chat.requests)
    assert world.small["id"] in s["models"]
    assert "llm.classify" in log  # the code's own log says what ran
    _, _, helpers = run_code(world.org, _user_id(world.admin), code, rerun=True)
    s = helpers.summary()["llm"]
    assert s["values_sent"] == 0 and s["cache_hits"] == len(set(COMMENTS)) and s["requests"] == 0


def test_the_per_run_value_cap_stops_before_any_request(world, fake_chat):
    _set(world.client, world.t, world.org, sandbox_llm_max_values=5)
    code = '''
def generate_df(ds_clients, excel_files, llm):
    return pd.DataFrame({"label": llm.classify([f"v{i}" for i in range(6)], ["a", "b"])})
'''
    with pytest.raises(Exception) as ei:
        run_code(world.org, _user_id(world.admin), code)
    msg = str(ei.value)
    assert "6" in msg and "5" in msg
    assert fake_chat.requests == []


def test_llm_is_unavailable_until_the_org_turns_it_on(world, fake_chat):
    _set(world.client, world.t, world.org, enable_sandbox_llm=False)
    code = '''
def generate_df(ds_clients, excel_files, llm):
    return pd.DataFrame({"label": llm.classify(["x"], ["a", "b"])})
'''
    from app.ai.code_execution.sandbox_helpers import HelperUnavailableError
    with pytest.raises(HelperUnavailableError):
        run_code(world.org, _user_id(world.admin), code)
    assert fake_chat.requests == []


def test_many_distinct_values_go_out_in_batches_and_every_one_is_answered(world, fake_chat, sandbox_mode):
    from app.ai.code_execution.sandbox_llm import CHAT_BATCH_ITEMS
    n = CHAT_BATCH_ITEMS * 2 + 37
    fake_chat.answer = lambda v, p: "a" if int(v[1:]) % 2 else "b"
    code = f'''
def generate_df(ds_clients, excel_files, llm):
    df = pd.DataFrame({{"v": [f"v{{i}}" for i in range({n})]}})
    df["label"] = llm.classify(df["v"], ["a", "b"])
    return df
'''
    df, _, _ = run_code(world.org, _user_id(world.admin), code)
    assert df["label"].tolist() == ["a" if i % 2 else "b" for i in range(n)]
    assert len(fake_chat.requests) == -(-n // CHAT_BATCH_ITEMS)
    assert max(len(r["items"]) for r in fake_chat.requests) <= CHAT_BATCH_ITEMS


def test_items_a_reply_leaves_out_are_asked_again_once(world, fake_chat):
    asked: dict = {}

    def answer(v, p):
        asked[v] = asked.get(v, 0) + 1
        # "skip" is never answered validly; "late" only on the second ask.
        if v == "skip" or (v == "late" and asked[v] == 1):
            return "not-a-label"
        return "a"

    fake_chat.answer = answer
    code = '''
def generate_df(ds_clients, excel_files, llm):
    return pd.DataFrame({"label": llm.classify(["ok", "late", "skip"], ["a", "b"])})
'''
    df, _, helpers = run_code(world.org, _user_id(world.admin), code)
    assert df["label"].tolist()[:2] == ["a", "a"] and df["label"][2] is None
    assert asked == {"ok": 1, "late": 2, "skip": 2}
    assert len(fake_chat.requests) == 2
    # Malformed twice is the model's answer: final, not a failure to retry.
    assert helpers.summary()["llm"]["declined_values"] == 1
    assert helpers.summary()["llm"]["failed_values"] == 0


def test_an_explicit_null_is_final_and_a_refresh_does_not_ask_again(world, fake_chat):
    fake_chat.answer = lambda v, p: None if v == "???" else "a"
    code = """
def generate_df(ds_clients, excel_files, llm):
    return pd.DataFrame({"label": llm.classify(["fine", "???"], ["a", "b"])})
"""
    df, _, helpers = run_code(world.org, _user_id(world.admin), code)
    assert df["label"].tolist() == ["a", None]
    assert len(fake_chat.requests) == 1  # the null was not retried
    s = helpers.summary()["llm"]
    assert s["declined_values"] == 1 and s["failed_values"] == 0
    _, _, helpers = run_code(world.org, _user_id(world.admin), code, rerun=True)
    assert len(fake_chat.requests) == 1
    assert helpers.summary()["llm"]["cache_hits"] == 2


def test_a_provider_error_is_not_cached_so_a_refresh_asks_again(world, fake_chat, monkeypatch):
    fake_chat.answer = lambda v, p: "a"
    from app.ai.llm.clients import anthropic_client
    real = anthropic_client.Anthropic.inference
    calls = {"n": 0}

    def flaky(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("bad request")  # not transient: no client-level retry
        return real(self, *a, **kw)

    monkeypatch.setattr(anthropic_client.Anthropic, "inference", flaky)
    code = """
def generate_df(ds_clients, excel_files, llm):
    return pd.DataFrame({"label": llm.classify(["x", "y"], ["a", "b"])})
"""
    with pytest.raises(Exception, match="failed"):
        run_code(world.org, _user_id(world.admin), code)
    df, _, _ = run_code(world.org, _user_id(world.admin), code, rerun=True)
    assert df["label"].tolist() == ["a", "a"]

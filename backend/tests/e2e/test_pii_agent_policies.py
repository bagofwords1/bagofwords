"""E2E: PII policies per agent (docs/design/pii-agent-policies.md).

Through the real settings API, the agent's own endpoint and the real LLM class
(only the provider client is stubbed):

* an agent's policy reaches every prompt of a run that involves the agent —
  including the other agents' data — and nothing else;
* the run's agent scope comes from the usage attribution; an unknown scope
  applies every agent policy (fail closed);
* agent admins set their agent's policy from the agent; turning it off
  (``:none``) is for org admins only; members without ``manage`` get 403;
* unknown agent ids are rejected at save; changes are audited.

The permutation matrix itself (every combination of agent / provider / default
policies) is in tests/unit/test_pii_agent_policies.py.
"""

import asyncio
import uuid

import pytest

EMAIL = "jane.roe@contoso.com"
PHONE = "415-555-0134"
TEXT = f"Contact {EMAIL} or call {PHONE}."
OFF = {rid: {"enabled": False} for rid in ("email", "credit_card", "us_ssn", "phone", "ipv4", "iban", "aws_access_key")}


@pytest.fixture(autouse=True)
def _force_pii_license():
    from app.ee import license as ee_license
    saved = (ee_license._cached_license, ee_license._cache_initialized)
    ee_license._cached_license = ee_license.LicenseInfo(
        licensed=True, tier="enterprise", org_name="pii-agent-tests",
        features=["pii_protection", "audit_logs"], license_id="pii-agent-test",
    )
    ee_license._cache_initialized = True
    try:
        yield
    finally:
        ee_license._cached_license, ee_license._cache_initialized = saved


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def world(create_user, login_user, whoami, create_data_source, tmp_path):
    import sqlite3
    db_file = tmp_path / "agents.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute("CREATE TABLE customers (id INTEGER, email TEXT)")
    email = f"pii_agent_{uuid.uuid4().hex[:8]}@acme.com"
    create_user(email=email)
    token = login_user(email, "test123")
    org_id = whoami(token)["organizations"][0]["id"]
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)
    H = _headers(token, org_id)
    prov = client.post("/api/llm/providers", json={
        "name": "public api", "provider_type": "custom",
        "credentials": {"base_url": "https://llm.example.com/v1", "api_key": "sk-test"},
        "models": [{"model_id": f"m-{uuid.uuid4().hex[:6]}", "name": "M", "is_custom": True}],
    }, headers=H)
    assert prov.status_code in (200, 201), prov.text

    def agent(name):
        return create_data_source(
            name=f"{name}-{uuid.uuid4().hex[:4]}", type="sqlite",
            config={"database": str(db_file)}, user_token=token, org_id=org_id,
        )["id"]

    return {
        "client": client, "H": H, "org": org_id, "provider": prov.json()["id"],
        "hr": agent("hr"), "sales": agent("sales"), "ops": agent("ops"),
        "create_user": create_user, "login_user": login_user,
    }


def _put_pii(w, **cfg):
    return w["client"].put("/api/organization/settings", json={"config": {"pii_protection": cfg}}, headers=w["H"])


def _base(**extra):
    return {
        "enabled": True,
        "policies": {
            "default": {"name": "Default", "mode": "replace", "builtin_overrides": OFF},
            "emails": {"name": "Emails", "mode": "replace",
                       "builtin_overrides": {**OFF, "email": {"enabled": True}}},
            "phones": {"name": "Phones", "mode": "replace",
                       "builtin_overrides": {**OFF, "phone": {"enabled": True}}},
            "no_emails": {"name": "No emails", "mode": "replace",
                          "builtin_overrides": {**OFF, "email": {"enabled": True, "action": "block"}}},
        },
        **extra,
    }


_UNSET = object()


def _send(provider_id, scopes, text=TEXT):
    """Send ``text`` through ONE real LLM instance once per scope in
    ``scopes`` (a list of agent-id lists, or _UNSET for no attribution), as a
    run would. Returns what the provider received per call, or the block."""
    from sqlalchemy import select
    from app.ai.llm.llm import LLM
    from app.ai.llm.pii.redactor import PiiPromptBlockedError
    from app.ai.llm.types import Message
    from app.ai.llm.usage_attribution import reset_usage_attribution, set_usage_attribution
    from app.dependencies import async_session_maker
    from app.models.llm_model import LLMModel

    async def run():
        async with async_session_maker() as session:
            model = (await session.execute(
                select(LLMModel).filter(LLMModel.provider_id == provider_id)
            )).scalars().first()
        llm = LLM(model, usage_session_maker=async_session_maker)
        seen = {}

        class StubClient:
            async def inference_stream_v2(self, *, messages, system=None, **kwargs):
                seen["text"] = messages[-1].content
                return
                yield  # pragma: no cover

        llm.client = StubClient()
        out = []
        for scope in scopes:
            token = set_usage_attribution(None if scope is _UNSET else {"data_source_ids": list(scope)})
            try:
                async for _ in llm.inference_stream_v2(
                    messages=[Message(role="user", content=text)], system="sys", should_record=False,
                ):
                    pass
                out.append(seen.pop("text"))
            except PiiPromptBlockedError as e:
                out.append(e)
            finally:
                reset_usage_attribution(token)
        return out

    return asyncio.run(run())


@pytest.mark.e2e
def test_the_agents_in_a_run_decide_the_policy_of_every_prompt(world):
    import app.ai.llm.pii.loader as loader
    from app.ai.llm.pii.redactor import PiiPromptBlockedError

    w = world
    put = _put_pii(w, **_base(agent_policies={w["hr"]: "emails", w["ops"]: "phones"}))
    assert put.status_code == 200, put.text
    loader.invalidate()

    hr, sales, ops = w["hr"], w["sales"], w["ops"]
    # One LLM instance, reused across scopes: the redactor follows the scope.
    got = _send(w["provider"], [[hr, sales], [sales], [], [hr, ops], _UNSET, [sales]])
    with_hr, sales_only, no_agents, hr_ops, unknown, sales_again = got
    assert EMAIL not in with_hr and PHONE in with_hr       # HR's policy covers Sales' data too
    assert sales_only == TEXT                               # no agent policy, Default is empty
    assert no_agents == TEXT
    assert EMAIL not in hr_ops and PHONE not in hr_ops     # both agents' policies
    assert EMAIL not in unknown and PHONE not in unknown   # unknown scope: every agent policy
    assert sales_again == TEXT                              # and back again

    # Default agent policy: Sales inherits it; HR keeps its own.
    put = _put_pii(w, **_base(agent_policies={hr: "emails"}, default_agent_policy="no_emails"))
    assert put.status_code == 200, put.text
    loader.invalidate()
    sales_only, hr_only = _send(w["provider"], [[sales], [hr]])
    assert isinstance(sales_only, PiiPromptBlockedError) and "No emails" in sales_only.policy_name
    assert EMAIL not in hr_only and not isinstance(hr_only, Exception)


@pytest.mark.e2e
def test_an_agent_policy_overrides_an_exempt_provider(world):
    import app.ai.llm.pii.loader as loader

    w = world
    put = _put_pii(w, **_base(agent_policies={w["hr"]: "emails"}, provider_policies={w["provider"]: "exempt"}))
    assert put.status_code == 200, put.text
    loader.invalidate()
    with_hr, without = _send(w["provider"], [[w["hr"]], [w["sales"]]])
    assert EMAIL not in with_hr
    assert without == TEXT


@pytest.mark.e2e
def test_settings_round_trip_and_reject_unknown_agents(world):
    w = world
    put = _put_pii(w, **_base(agent_policies={w["hr"]: "emails", w["sales"]: ":none"}, default_agent_policy="phones"))
    assert put.status_code == 200, put.text
    cfg = w["client"].get("/api/organization/settings", headers=w["H"]).json()["config"]["pii_protection"]
    assert cfg["agent_policies"] == {w["hr"]: "emails", w["sales"]: ":none"}
    assert cfg["default_agent_policy"] == "phones"

    assert _put_pii(w, **_base(agent_policies={str(uuid.uuid4()): "emails"})).status_code == 400
    assert _put_pii(w, **_base(agent_policies={w["hr"]: "exempt"})).status_code == 400
    assert _put_pii(w, **_base(default_agent_policy="missing")).status_code == 400


def _member(w, *, manage_agent=None):
    email = f"pii_agent_m_{uuid.uuid4().hex[:8]}@acme.com"
    invite = w["client"].post(f"/api/organizations/{w['org']}/members",
                              json={"organization_id": w["org"], "email": email, "role": "member"}, headers=w["H"])
    assert invite.status_code == 200, invite.text
    w["create_user"](email=email)
    token = w["login_user"](email, "test123")
    if manage_agent:
        me = w["client"].get("/api/users/whoami", headers=_headers(token, w["org"])).json()
        grant = w["client"].post(f"/api/organizations/{w['org']}/resource-grants", json={
            "resource_type": "data_source", "resource_id": manage_agent,
            "principal_type": "user", "principal_id": me["id"], "permissions": ["manage"],
        }, headers=w["H"])
        assert grant.status_code == 200, grant.text
    return _headers(token, w["org"])


@pytest.mark.e2e
def test_agent_admins_set_their_agents_policy_from_the_agent(world):
    import app.ai.llm.pii.loader as loader

    w = world
    assert _put_pii(w, **_base(default_agent_policy="phones")).status_code == 200
    url = f"/api/data_sources/{w['hr']}/pii_policy"
    agent_admin = _member(w, manage_agent=w["hr"])
    member = _member(w)

    got = w["client"].get(url, headers=agent_admin)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["policy"] == ":inherit" and body["effective_policy"] == "phones"
    assert body["can_turn_off"] is False
    assert {p["id"] for p in body["policies"]} >= {"default", "emails", "phones"}

    put = w["client"].put(url, json={"policy": "emails"}, headers=agent_admin)
    assert put.status_code == 200, put.text
    assert put.json()["effective_policy"] == "emails"
    cfg = w["client"].get("/api/organization/settings", headers=w["H"]).json()["config"]["pii_protection"]
    assert cfg["agent_policies"] == {w["hr"]: "emails"}
    loader.invalidate()
    (with_hr,) = _send(w["provider"], [[w["hr"]]])
    assert EMAIL not in with_hr

    # Only org admins may turn it off; no Exempt; no unknown policy.
    assert w["client"].put(url, json={"policy": ":none"}, headers=agent_admin).status_code == 403
    assert w["client"].put(url, json={"policy": "exempt"}, headers=agent_admin).status_code == 400
    assert w["client"].put(url, json={"policy": "missing"}, headers=agent_admin).status_code == 400
    off = w["client"].put(url, json={"policy": ":none"}, headers=w["H"])
    assert off.status_code == 200 and off.json()["effective_policy"] == ":none"

    # Back to the org default.
    back = w["client"].put(url, json={"policy": ":inherit"}, headers=agent_admin)
    assert back.status_code == 200 and back.json()["policy"] == ":inherit"

    # A member without manage on the agent can't read or write it, nor another agent.
    assert w["client"].get(url, headers=member).status_code == 403
    assert w["client"].put(url, json={"policy": "emails"}, headers=member).status_code == 403
    other = f"/api/data_sources/{w['sales']}/pii_policy"
    assert w["client"].put(other, json={"policy": "emails"}, headers=agent_admin).status_code == 403

    logs = w["client"].get("/api/enterprise/audit",
                           params={"action": "pii_protection.agent_policies_updated"}, headers=w["H"])
    assert logs.status_code == 200, logs.text
    items = [i for i in logs.json()["items"] if i["action"] == "pii_protection.agent_policies_updated"]
    assert any(c["to"] == "emails" for i in items for c in i["details"]["changes"])


@pytest.mark.e2e
def test_two_managers_saving_different_agents_at_once_both_keep_their_choice(world, monkeypatch):
    """Two saves read the same settings revision; the first to write wins the
    row, the second re-reads and re-applies its change on top, so neither
    assignment is lost (previously the later commit erased the earlier one)."""
    import app.services.pii_agent_policy_service as svc
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.user import User

    w = world
    assert _put_pii(w, **_base()).status_code == 200
    real_read = svc._read_settings_row
    state = {"interleaved": False}

    async def run():
        async with async_session_maker() as s1:
            org = (await s1.execute(select(Organization).where(Organization.id == w["org"]))).scalar_one()
            admin = (await s1.execute(select(User).limit(1))).scalar_one()
            admin_id = admin.id

            async def read_then_let_the_other_save_first(db, org_id):
                row = await real_read(db, org_id)
                if not state["interleaved"]:
                    state["interleaved"] = True
                    # A second manager saves another agent from a separate
                    # session after we read and before we write.
                    async with async_session_maker() as s2:
                        org2 = (await s2.execute(select(Organization).where(Organization.id == w["org"]))).scalar_one()
                        admin2 = (await s2.execute(select(User).where(User.id == admin_id))).scalar_one()
                        await svc.set_agent_pii_policy(s2, org2, admin2, w["sales"], "phones")
                return row

            monkeypatch.setattr(svc, "_read_settings_row", read_then_let_the_other_save_first)
            return await svc.set_agent_pii_policy(s1, org, admin, w["hr"], "emails")

    view = asyncio.run(run())
    assert state["interleaved"]
    assert view["effective_policy"] == "emails"
    cfg = w["client"].get("/api/organization/settings", headers=w["H"]).json()["config"]["pii_protection"]
    assert cfg["agent_policies"] == {w["hr"]: "emails", w["sales"]: "phones"}


# --- earlier turns keep their agents' policies --------------------------------
# A follow-up run reloads the report's history, which can hold rows from an
# agent since detached. The run's scope adds every agent exposed to the
# report's earlier runs; when that provenance is incomplete the scope is left
# unknown (every agent policy applies).

def _history(w, report_agents, turns):
    """A report with ``report_agents`` attached now and earlier ``turns``:
    each turn is a list of exposed agent ids, None for a turn with a run but
    no exposure, or "no-run" for a turn with no recorded run."""
    from datetime import datetime
    from app.dependencies import async_session_maker
    from app.models.agent_execution import AgentExecution, execution_exposed_agents as exposed
    from app.models.completion import Completion

    rep = w["client"].post("/api/reports", json={"title": "history", "widget": None, "files": [],
                                                   "data_sources": report_agents}, headers=w["H"])
    assert rep.status_code == 200, rep.text
    rid = rep.json()["id"]

    async def seed():
        async with async_session_maker() as s:
            for i, turn in enumerate(turns):
                c = Completion(report_id=rid, role="system", prompt={}, completion={"content": "rows"},
                               turn_index=i, sigkill=datetime.utcnow(), status="success")
                s.add(c)
                await s.flush()
                if turn == "no-run":
                    continue
                ex = AgentExecution(completion_id=c.id, organization_id=w["org"], report_id=rid, status="success")
                s.add(ex)
                await s.flush()
                for agent_id in turn or []:
                    await s.execute(exposed.insert().values(execution_id=ex.id, agent_id=agent_id))
            await s.commit()

    asyncio.run(seed())
    return rid


def _historical(rid):
    from app.dependencies import async_session_maker
    from app.ai.llm.pii.history_scope import historical_agent_ids

    async def run():
        async with async_session_maker() as s:
            return await historical_agent_ids(s, rid)

    return asyncio.run(run())


@pytest.mark.e2e
def test_a_detached_agents_earlier_turns_keep_its_policy_in_the_next_turn(world):
    import app.ai.llm.pii.loader as loader

    w = world
    assert _put_pii(w, **_base(agent_policies={w["hr"]: "emails"},
                                provider_policies={w["provider"]: "exempt"})).status_code == 200
    loader.invalidate()
    # Turn 1 ran with HR + Sales; HR has since been detached: only Sales now.
    rid = _history(w, [w["sales"]], [[w["hr"], w["sales"]]])
    hist = _historical(rid)
    assert hist == sorted([w["hr"], w["sales"]])
    # The second turn's scope = current agents + historical ones: HR's policy
    # still applies, even on an Exempt provider.
    (second_turn,) = _send(w["provider"], [sorted({w["sales"], *hist})])
    assert EMAIL not in second_turn
    # Without the history (the old behavior) it would have gone out raw.
    (current_only,) = _send(w["provider"], [[w["sales"]]])
    assert current_only == TEXT


@pytest.mark.e2e
@pytest.mark.parametrize("turns", [
    [None],                       # a run whose exposure was never written (before exposure, or a failed write)
    ["no-run"],                   # an earlier turn with no recorded run at all
    [["{sales}"], None],          # complete first turn, incomplete second
], ids=["run-without-exposure", "turn-without-run", "partly-recorded"])
def test_incomplete_history_leaves_the_scope_unknown(world, turns):
    w = world
    turns = [[w["sales"]] if t == ["{sales}"] else t for t in turns]
    rid = _history(w, [w["sales"]], turns)
    assert _historical(rid) is None  # → unknown scope: every agent policy applies


@pytest.mark.e2e
def test_a_new_report_and_a_fork_have_the_expected_history(world):
    w = world
    assert _historical(_history(w, [w["sales"]], [])) == []
    rid = _history(w, [w["sales"]], [[w["sales"]]])
    assert _historical(rid) == [w["sales"]]

    from app.dependencies import async_session_maker
    from app.models.report import Report

    async def fork_of(source):
        async with async_session_maker() as s:
            r = await s.get(Report, source)
            r.forked_from_id = source  # any report forked from another
            await s.commit()

    asyncio.run(fork_of(rid))
    assert _historical(rid) is None

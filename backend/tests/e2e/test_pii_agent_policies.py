"""E2E: PII policies per agent (docs/design/pii-agent-policies.md).

Through the real settings API, the agent's own endpoint and the real LLM class
(only the provider client is stubbed):

* an agent's policy reaches every prompt of a run that involves the agent —
  including the other agents' data — and nothing else;
* the run's agent scope comes from the usage attribution; an unknown scope
  applies every agent policy (fail closed);
* agent admins set their agent's policy from the agent; turning it off
  (``none``) is for org admins only; members without ``manage`` get 403;
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
    put = _put_pii(w, **_base(agent_policies={w["hr"]: "emails", w["sales"]: "none"}, default_agent_policy="phones"))
    assert put.status_code == 200, put.text
    cfg = w["client"].get("/api/organization/settings", headers=w["H"]).json()["config"]["pii_protection"]
    assert cfg["agent_policies"] == {w["hr"]: "emails", w["sales"]: "none"}
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
    assert body["policy"] == "inherit" and body["effective_policy"] == "phones"
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
    assert w["client"].put(url, json={"policy": "none"}, headers=agent_admin).status_code == 403
    assert w["client"].put(url, json={"policy": "exempt"}, headers=agent_admin).status_code == 400
    assert w["client"].put(url, json={"policy": "missing"}, headers=agent_admin).status_code == 400
    off = w["client"].put(url, json={"policy": "none"}, headers=w["H"])
    assert off.status_code == 200 and off.json()["effective_policy"] == "none"

    # Back to the org default.
    back = w["client"].put(url, json={"policy": "inherit"}, headers=agent_admin)
    assert back.status_code == 200 and back.json()["policy"] == "inherit"

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

"""E2E: PII protection for outbound LLM prompts (enterprise `pii_protection`).

Covers the promises that matter:
  * the config round-trips through the settings API and normalizes via schema,
  * an invalid custom regex is rejected at save time (400),
  * the dry-run endpoint previews replace + block behavior,
  * enterprise gating: with the feature OFF, the settings write is refused (402)
    AND the redactor loader returns None even for an enabled config — i.e. the
    feature cannot activate in a community build,
  * the LLM chokepoint actually redacts: planted PII in a prompt never reaches
    the underlying provider client (replace), and block mode refuses the call.

The whole e2e session runs with a forced enterprise license (see
tests/e2e/conftest.py), so the "community" leg explicitly monkeypatches
`has_feature` to False.
"""

import asyncio
import json
import types
import uuid

import pytest


@pytest.fixture(autouse=True)
def _force_pii_license():
    """Guarantee `pii_protection` is licensed for every test here, independent
    of suite ordering. The session-wide enterprise fixture (conftest) also lists
    it, but another module can reset the license cache mid-suite — this makes the
    PII tests self-sufficient (same pattern as test_connection_rate_limit)."""
    from app.ee import license as ee_license
    saved_cached = ee_license._cached_license
    saved_initialized = ee_license._cache_initialized
    ee_license._cached_license = ee_license.LicenseInfo(
        licensed=True,
        tier="enterprise",
        org_name="pii-tests",
        features=["pii_protection"],
        license_id="pii-test",
    )
    ee_license._cache_initialized = True
    try:
        yield
    finally:
        ee_license._cached_license = saved_cached
        ee_license._cache_initialized = saved_initialized


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _fake_model(org_id, provider_type="anthropic"):
    """A minimal stand-in for LLMModel sufficient to construct LLM without any
    network. The real provider client is swapped for a stub in each test."""
    provider = types.SimpleNamespace(
        provider_type=provider_type,
        additional_config={},
        decrypt_credentials=lambda: ["fake-key"],
    )
    return types.SimpleNamespace(
        model_id="fake-model",
        provider=provider,
        organization_id=str(org_id),
        supports_vision=False,
    )


# --- settings API ----------------------------------------------------------

@pytest.mark.e2e
def test_pii_config_roundtrips_and_validates(create_user, login_user, whoami):
    email = f"pii_{uuid.uuid4().hex[:8]}@acme.com"
    create_user(email=email)
    token = login_user(email, "test123")
    org_id = whoami(token)["organizations"][0]["id"]

    from main import app
    from fastapi.testclient import TestClient
    client = TestClient(app)
    H = _headers(token, org_id)

    # Enable with a custom multi-pattern rule.
    put = client.put("/api/organization/settings", json={"config": {"pii_protection": {
        "enabled": True,
        "mode": "replace",
        "custom_rules": [
            {"id": "emp", "name": "Employee ID", "patterns": [r"EMP-\d{4}", r"E\d{6}"],
             "replacement": "[EMP]", "enabled": True},
        ],
        "builtin_overrides": {"ipv4": {"enabled": False}},
    }}}, headers=H)
    assert put.status_code == 200, put.text
    saved = put.json()["config"]["pii_protection"]
    assert saved["enabled"] is True
    assert saved["mode"] == "replace"
    assert saved["custom_rules"][0]["patterns"] == [r"EMP-\d{4}", r"E\d{6}"]
    assert saved["builtin_overrides"]["ipv4"]["enabled"] is False

    # Bad regex is rejected.
    bad = client.put("/api/organization/settings", json={"config": {"pii_protection": {
        "enabled": True,
        "custom_rules": [{"id": "x", "name": "X", "patterns": ["(unclosed"], "replacement": "[X]"}],
    }}}, headers=H)
    assert bad.status_code == 400, bad.text


@pytest.mark.e2e
def test_pii_dry_run_endpoint_preview(create_user, login_user, whoami):
    email = f"pii_{uuid.uuid4().hex[:8]}@acme.com"
    create_user(email=email)
    token = login_user(email, "test123")
    org_id = whoami(token)["organizations"][0]["id"]

    from main import app
    from fastapi.testclient import TestClient
    client = TestClient(app)
    H = _headers(token, org_id)

    # Replace preview
    r = client.post("/api/organization/pii/test", json={
        "text": "email a@b.com and ssn 078-05-1120",
        "config": {"mode": "replace"},
    }, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "a@b.com" not in body["text"]
    assert body["blocked"] is False
    assert {m["id"] for m in body["matches"]} >= {"email", "us_ssn"}

    # Block preview
    rb = client.post("/api/organization/pii/test", json={
        "text": "email a@b.com",
        "config": {"mode": "block"},
    }, headers=H)
    assert rb.status_code == 200, rb.text
    assert rb.json()["blocked"] is True

    # Builtin-rule catalogue is exposed for the settings page
    rc = client.get("/api/organization/pii/builtin-rules", headers=H)
    assert rc.status_code == 200
    ids = {r["id"] for r in rc.json()["rules"]}
    assert {"email", "credit_card", "us_ssn", "phone"} <= ids


# --- enterprise gating -----------------------------------------------------

@pytest.mark.e2e
def test_pii_write_refused_without_license(create_user, login_user, whoami, monkeypatch):
    email = f"pii_{uuid.uuid4().hex[:8]}@acme.com"
    create_user(email=email)
    token = login_user(email, "test123")
    org_id = whoami(token)["organizations"][0]["id"]

    from main import app
    from fastapi.testclient import TestClient
    client = TestClient(app)
    H = _headers(token, org_id)

    # Community build: feature off everywhere.
    import app.services.organization_settings_service as svc
    monkeypatch.setattr(svc, "has_feature", lambda feature: False)

    resp = client.put("/api/organization/settings", json={"config": {"pii_protection": {
        "enabled": True, "mode": "replace",
    }}}, headers=H)
    assert resp.status_code == 402, resp.text


@pytest.mark.e2e
def test_redactor_loader_is_enterprise_gated(create_user, login_user, whoami, monkeypatch):
    """The loader is the single enforcement point the LLM uses. Even with an
    enabled config persisted, it must return None on a community instance."""
    email = f"pii_{uuid.uuid4().hex[:8]}@acme.com"
    create_user(email=email)
    token = login_user(email, "test123")
    org_id = whoami(token)["organizations"][0]["id"]

    from main import app
    from fastapi.testclient import TestClient
    from app.dependencies import async_session_maker
    import app.ai.llm.pii.loader as loader

    client = TestClient(app)
    H = _headers(token, org_id)

    # Persist an enabled config (licensed session lets the write through).
    client.put("/api/organization/settings", json={"config": {"pii_protection": {
        "enabled": True, "mode": "replace",
    }}}, headers=H)

    async def load():
        return await loader.load_redactor_for_org(org_id, async_session_maker)

    # Licensed -> active redactor.
    loader.invalidate()
    redactor = asyncio.run(load())
    assert redactor is not None and redactor.active

    # Community -> None, despite the enabled config in the DB.
    monkeypatch.setattr(loader, "has_feature", lambda feature: False)
    loader.invalidate()
    assert asyncio.run(load()) is None


# --- the chokepoint actually redacts --------------------------------------

@pytest.mark.e2e
def test_inference_redacts_before_reaching_provider():
    # Inject the redactor directly to isolate the chokepoint wiring from the
    # loader (loader gating is covered separately above).
    from app.ai.llm.llm import LLM
    from app.ai.llm.pii.redactor import build_redactor

    redactor = build_redactor({"enabled": True, "mode": "replace"})
    model = _fake_model("org-x")
    llm = LLM(model, pii_redactor=redactor)

    captured = {}

    class StubClient:
        def inference(self, model_id, prompt, images=None, system=None):
            captured["prompt"] = prompt
            return "ok"

    llm.client = StubClient()

    out = llm.inference(
        "Reach the customer at john@acme.com or 415-555-1234.",
        should_record=False,
    )
    assert out == "ok"
    assert "john@acme.com" not in captured["prompt"]
    assert "415-555-1234" not in captured["prompt"]
    assert "[REDACTED_EMAIL]" in captured["prompt"]


@pytest.mark.e2e
def test_per_rule_block_via_dry_run(create_user, login_user, whoami):
    """A rule set to 'block' refuses the request even when the workspace
    default is 'replace' — exercised through the preview endpoint."""
    email = f"pii_{uuid.uuid4().hex[:8]}@acme.com"
    create_user(email=email)
    token = login_user(email, "test123")
    org_id = whoami(token)["organizations"][0]["id"]

    from main import app
    from fastapi.testclient import TestClient
    client = TestClient(app)
    H = _headers(token, org_id)

    cfg = {"mode": "replace", "builtin_overrides": {"us_ssn": {"action": "block"}}}
    # SSN present -> blocked
    r = client.post("/api/organization/pii/test", json={"text": "ssn 078-05-1120", "config": cfg}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["blocked"] is True
    # email only (no ssn) -> replaced, not blocked
    r2 = client.post("/api/organization/pii/test", json={"text": "email a@b.com", "config": cfg}, headers=H)
    assert r2.json()["blocked"] is False
    assert "a@b.com" not in r2.json()["text"]


@pytest.mark.e2e
def test_inference_stream_v2_block_mode_refuses(create_user, login_user, whoami):
    from app.ai.llm.llm import LLM
    from app.ai.llm.pii.redactor import build_redactor, PiiPromptBlockedError
    from app.ai.llm.types import Message

    redactor = build_redactor({"enabled": True, "mode": "block"})
    llm = LLM(_fake_model("org-y"), pii_redactor=redactor)

    async def drive():
        agen = llm.inference_stream_v2(
            messages=[Message(role="user", content="my ssn is 078-05-1120")],
            system="you are helpful",
            should_record=False,
        )
        # Consuming the generator must raise before any provider call.
        async for _ in agen:
            pass

    with pytest.raises(PiiPromptBlockedError):
        asyncio.run(drive())


# --- per-provider exemption -------------------------------------------------
#
# An org can name LLM providers (e.g. a self-hosted model) that receive prompts
# unredacted; every other provider keeps the org policy. The exemption is
# resolved per LLM instance from the provider it actually calls, so a fallback
# to another provider is judged by that provider, not the one first asked.


def _admin(create_user, login_user, whoami):
    email = f"pii_{uuid.uuid4().hex[:8]}@acme.com"
    create_user(email=email)
    token = login_user(email, "test123")
    org_id = whoami(token)["organizations"][0]["id"]
    from main import app
    from fastapi.testclient import TestClient
    return TestClient(app), _headers(token, org_id), org_id


def _create_custom_provider(client, H, name):
    resp = client.post("/api/llm/providers", json={
        "name": name,
        "provider_type": "custom",
        "credentials": {"base_url": "https://llm.example.com/v1", "api_key": "sk-test"},
        "models": [{"model_id": f"m-{uuid.uuid4().hex[:6]}", "name": "M", "is_custom": True}],
    }, headers=H)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _put_pii(client, H, **cfg):
    return client.put("/api/organization/settings", json={"config": {"pii_protection": cfg}}, headers=H)


def _prompt_sent_through(provider_id):
    """Build the real LLM for ``provider_id``'s model (loader-resolved PII, no
    injected redactor), stub only the provider client, and return the user
    message text that reached it."""
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.llm_model import LLMModel
    from app.ai.llm.llm import LLM
    from app.ai.llm.types import Message

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
                yield  # pragma: no cover - makes this an async generator

        llm.client = StubClient()
        async for _ in llm.inference_stream_v2(
            messages=[Message(role="user", content="Email jane.roe@contoso.com about it")],
            system="sys",
            should_record=False,
        ):
            pass
        return seen["text"]

    return asyncio.run(run())


@pytest.mark.e2e
def test_exempt_provider_gets_raw_prompt_and_others_keep_org_policy(create_user, login_user, whoami):
    import app.ai.llm.pii.loader as loader

    client, H, _ = _admin(create_user, login_user, whoami)
    trusted = _create_custom_provider(client, H, "in-vpc model")
    public = _create_custom_provider(client, H, "public api")

    put = _put_pii(client, H, enabled=True, mode="replace", exempt_provider_ids=[trusted])
    assert put.status_code == 200, put.text
    assert put.json()["config"]["pii_protection"]["exempt_provider_ids"] == [trusted]
    loader.invalidate()

    assert "jane.roe@contoso.com" in _prompt_sent_through(trusted)
    redacted = _prompt_sent_through(public)
    assert "jane.roe@contoso.com" not in redacted

    # Removing the exemption puts the provider back under the org policy.
    assert _put_pii(client, H, exempt_provider_ids=[]).status_code == 200
    assert "jane.roe@contoso.com" not in _prompt_sent_through(trusted)


@pytest.mark.e2e
def test_exemption_does_not_relax_display_masking(create_user, login_user, whoami):
    """Display surfaces resolve the redactor without a provider: an exemption
    only changes what one provider receives, never what the UI shows."""
    import app.ai.llm.pii.loader as loader
    from app.dependencies import async_session_maker

    client, H, org_id = _admin(create_user, login_user, whoami)
    trusted = _create_custom_provider(client, H, "in-vpc model")
    assert _put_pii(client, H, enabled=True, exempt_provider_ids=[trusted]).status_code == 200
    loader.invalidate()

    display = asyncio.run(loader.load_redactor_for_org(org_id, async_session_maker))
    assert display is not None and display.active
    outbound = asyncio.run(loader.load_redactor_for_org(org_id, async_session_maker, trusted))
    assert outbound is None


@pytest.mark.e2e
def test_exempt_list_only_accepts_this_orgs_live_providers(
    create_user, login_user, whoami, create_organization, monkeypatch
):
    from app.settings.config import settings as bow_settings
    client, H, _ = _admin(create_user, login_user, whoami)
    # The org's first model becomes its default, which cannot be deleted.
    _create_custom_provider(client, H, "default")
    mine = _create_custom_provider(client, H, "mine")

    # A second, unrelated org with its own provider.
    monkeypatch.setattr(bow_settings.bow_config.features, "allow_uninvited_signups", True)
    monkeypatch.setattr(bow_settings.bow_config.features, "allow_multiple_organizations", True)
    other_email = f"pii_{uuid.uuid4().hex[:8]}@other.com"
    create_user(email=other_email)
    other_token = login_user(other_email, "test123")
    other_org = create_organization(name=f"other-{uuid.uuid4().hex[:6]}", user_token=other_token)
    theirs = _create_custom_provider(client, _headers(other_token, other_org), "theirs")

    # Unknown ids and another org's provider are rejected, and nothing is saved.
    for bad in ([f"nope-{uuid.uuid4().hex[:6]}"], [theirs], "not-a-list", [mine, 7]):
        resp = _put_pii(client, H, enabled=True, exempt_provider_ids=bad)
        assert resp.status_code == 400, (bad, resp.text)
    cfg = client.get("/api/organization/settings", headers=H).json()["config"].get("pii_protection") or {}
    assert not cfg.get("exempt_provider_ids")

    # Duplicates collapse.
    ok = _put_pii(client, H, enabled=True, exempt_provider_ids=[mine, mine])
    assert ok.status_code == 200, ok.text
    assert ok.json()["config"]["pii_protection"]["exempt_provider_ids"] == [mine]

    # A stored exemption whose provider was deleted never blocks later saves;
    # it is dropped on the next write.
    assert client.delete(f"/api/llm/providers/{mine}", headers=H).status_code in (200, 204)
    later = _put_pii(client, H, enabled=True, exempt_provider_ids=[mine], mode="block")
    assert later.status_code == 200, later.text
    saved = later.json()["config"]["pii_protection"]
    assert saved["mode"] == "block"
    assert saved["exempt_provider_ids"] == []


@pytest.mark.e2e
def test_changing_exemptions_is_audited_with_the_providers(create_user, login_user, whoami):
    from app.ee import license as ee_license
    # Reading the audit log is its own enterprise feature; the autouse license
    # above is rebuilt per test, so extending it here is test-local.
    ee_license._cached_license.features.append("audit_logs")
    client, H, _ = _admin(create_user, login_user, whoami)
    a = _create_custom_provider(client, H, "a")
    b = _create_custom_provider(client, H, "b")
    action = "pii_protection.provider_policies_updated"

    def entries():
        resp = client.get("/api/enterprise/audit", params={"action": action}, headers=H)
        assert resp.status_code == 200, resp.text
        return [i for i in resp.json()["items"] if i["action"] == action]

    assert _put_pii(client, H, enabled=True, exempt_provider_ids=[a]).status_code == 200
    assert _put_pii(client, H, enabled=True, exempt_provider_ids=[b]).status_code == 200
    # A save that leaves the assignments unchanged adds no entry.
    assert _put_pii(client, H, enabled=True, mode="block", exempt_provider_ids=[b]).status_code == 200

    changes = [e["details"]["changes"] for e in entries()]
    assert len(changes) == 2
    assert [{"provider_id": a, "from": "default", "to": "exempt"}] in changes
    assert sorted([{"provider_id": a, "from": "exempt", "to": "default"},
                   {"provider_id": b, "from": "default", "to": "exempt"}], key=lambda c: c["provider_id"]) in [
        sorted(c, key=lambda x: x["provider_id"]) for c in changes
    ]


@pytest.mark.e2e
def test_member_cannot_change_exemptions(create_user, login_user, whoami):
    client, H, org_id = _admin(create_user, login_user, whoami)
    trusted = _create_custom_provider(client, H, "in-vpc model")

    email = f"pii_member_{uuid.uuid4().hex[:8]}@acme.com"
    invite = client.post(f"/api/organizations/{org_id}/members",
                         json={"organization_id": org_id, "email": email, "role": "member"}, headers=H)
    assert invite.status_code == 200, invite.text
    create_user(email=email)
    member_H = _headers(login_user(email, "test123"), org_id)

    resp = _put_pii(client, member_H, enabled=True, exempt_provider_ids=[trusted])
    assert resp.status_code == 403, resp.text
    cfg = client.get("/api/organization/settings", headers=H).json()["config"].get("pii_protection") or {}
    assert not cfg.get("exempt_provider_ids")


# --- named policies ------------------------------------------------------------
#
# Policies are option sets over one shared rule list; each LLM provider is
# assigned one (unassigned: Default; Exempt: nothing redacted). The tests write
# config through the settings API and send prompts through the real LLM class,
# stubbing only the provider client.

def _policies_payload(strict_provider=None, exempt_provider=None):
    return {
        "enabled": True,
        "custom_rules": [{"id": "emp", "name": "Employee ID", "patterns": [r"EMP-\d{4}"], "replacement": "[EMP]"}],
        "policies": {
            "default": {"name": "Default", "mode": "replace"},
            "strict": {"name": "Strict", "mode": "block"},
        },
        "provider_policies": {
            **({strict_provider: "strict"} if strict_provider else {}),
            **({exempt_provider: "exempt"} if exempt_provider else {}),
        },
    }


def _send_through(provider_id, text, *, drain_audit=False):
    """Send ``text`` as the user turn through the real LLM for the provider's
    model; return what the provider received, or the PII block raised."""
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.llm_model import LLMModel
    from app.ai.llm.llm import LLM
    from app.ai.llm.types import Message
    from app.ee.audit import tool_audit

    async def run():
        async with async_session_maker() as session:
            model = (await session.execute(
                select(LLMModel).filter(LLMModel.provider_id == provider_id)
            )).scalars().first()
        llm = LLM(model, usage_session_maker=async_session_maker)
        seen = {}

        class StubClient:
            async def inference_stream_v2(self, *, messages, system=None, **kwargs):
                seen["system"] = system
                seen["text"] = messages[-1].content
                return
                yield  # pragma: no cover

        llm.client = StubClient()
        try:
            async for _ in llm.inference_stream_v2(
                messages=[Message(role="user", content=text)], system="sys", should_record=False,
            ):
                pass
            return seen
        except Exception as exc:  # the PII block, returned for the assertions
            return exc
        finally:
            if drain_audit:
                await asyncio.sleep(0)
                await tool_audit.drain_tool_audit_queue(timeout=30)
                await tool_audit.stop_tool_audit_worker(timeout=30)

    return asyncio.run(run())


@pytest.mark.e2e
def test_each_provider_receives_what_its_policy_says(create_user, login_user, whoami):
    import app.ai.llm.pii.loader as loader
    from app.ai.llm.pii.redactor import PiiPromptBlockedError
    from app.ee import license as ee_license
    ee_license._cached_license.features.append("audit_logs")

    client, H, _ = _admin(create_user, login_user, whoami)
    default_p = _create_custom_provider(client, H, "public api")
    strict_p = _create_custom_provider(client, H, "strict api")
    vpc_p = _create_custom_provider(client, H, "in-vpc model")
    put = _put_pii(client, H, **_policies_payload(strict_provider=strict_p, exempt_provider=vpc_p))
    assert put.status_code == 200, put.text
    loader.invalidate()

    text = "Ping jane.roe@contoso.com about EMP-1234"
    got = _send_through(default_p, text)
    assert "jane.roe@contoso.com" not in got["text"] and "[EMP]" in got["text"]

    assert _send_through(vpc_p, text)["text"] == text

    blocked = _send_through(strict_p, text, drain_audit=True)
    assert isinstance(blocked, PiiPromptBlockedError)
    assert blocked.policy_id == "strict" and blocked.model  # names the policy and the model
    assert "Email address" in blocked.rule_names

    logs = client.get("/api/enterprise/audit", params={"action": "pii_protection.prompt_blocked"}, headers=H)
    assert logs.status_code == 200, logs.text
    items = [i for i in logs.json()["items"] if i["action"] == "pii_protection.prompt_blocked"]
    assert len(items) == 1
    assert items[0]["details"]["policy_id"] == "strict"
    assert "jane.roe@contoso.com" not in json.dumps(items[0])


@pytest.mark.e2e
def test_instructions_and_schema_reach_the_model_unredacted_but_the_prompt_does_not(create_user, login_user, whoami):
    import app.ai.llm.pii.loader as loader
    from app.ai.llm.pii.context import CONTEXT_END, CONTEXT_START, pii_context

    client, H, _ = _admin(create_user, login_user, whoami)
    p = _create_custom_provider(client, H, "public api")
    assert _put_pii(client, H, **_policies_payload()).status_code == 200
    loader.invalidate()

    text = (
        pii_context("<instructions>Escalations go to ops@corp.io</instructions>")
        + pii_context('<schema><column name="Email" example="sample@corp.io"/></schema>')
        + "<user_prompt>What did jane.roe@contoso.com order?</user_prompt>"
    )
    got = _send_through(p, text)["text"]
    assert "Escalations go to ops@corp.io" in got
    assert 'example="sample@corp.io"' in got
    assert "jane.roe@contoso.com" not in got
    assert CONTEXT_START not in got and CONTEXT_END not in got


@pytest.mark.e2e
@pytest.mark.parametrize("state", ["disabled", "exempt", "community"])
def test_context_markers_never_reach_a_provider(state, create_user, login_user, whoami, monkeypatch):
    import app.ai.llm.pii.loader as loader
    from app.ai.llm.pii.context import CONTEXT_END, CONTEXT_START, pii_context

    client, H, _ = _admin(create_user, login_user, whoami)
    p = _create_custom_provider(client, H, "public api")
    payload = _policies_payload(exempt_provider=p if state == "exempt" else None)
    payload["enabled"] = state != "disabled"
    assert _put_pii(client, H, **payload).status_code == 200
    if state == "community":
        monkeypatch.setattr(loader, "has_feature", lambda feature: False)
    loader.invalidate()

    got = _send_through(p, pii_context("instructions") + " ask")["text"]
    assert got == "instructions ask"
    assert CONTEXT_START not in got and CONTEXT_END not in got


@pytest.mark.e2e
def test_deleting_a_policy_moves_its_providers_back_to_default(create_user, login_user, whoami):
    client, H, _ = _admin(create_user, login_user, whoami)
    p = _create_custom_provider(client, H, "strict api")
    assert _put_pii(client, H, **_policies_payload(strict_provider=p)).status_code == 200

    payload = _policies_payload()
    del payload["policies"]["strict"]
    payload["provider_policies"] = {p: "strict"}
    # Assigning to a policy the same write deletes is refused...
    assert _put_pii(client, H, **payload).status_code == 400
    # ...deleting it without reassigning moves the provider back to Default.
    del payload["provider_policies"]
    saved = _put_pii(client, H, **payload)
    assert saved.status_code == 200, saved.text
    cfg = saved.json()["config"]["pii_protection"]
    assert "strict" not in cfg["policies"]
    assert p not in cfg["provider_policies"]


@pytest.mark.e2e
def test_policy_settings_round_trip_with_the_old_fields_mirrored(create_user, login_user, whoami):
    client, H, _ = _admin(create_user, login_user, whoami)
    p = _create_custom_provider(client, H, "in-vpc model")
    assert _put_pii(client, H, **_policies_payload(exempt_provider=p)).status_code == 200
    cfg = client.get("/api/organization/settings", headers=H).json()["config"]["pii_protection"]
    assert set(cfg["policies"]) == {"default", "strict"}
    assert cfg["policies"]["strict"]["mode"] == "block"
    assert cfg["provider_policies"] == {p: "exempt"}
    # Mirrors for clients that only know one rule set:
    assert cfg["mode"] == "replace" and cfg["exempt_provider_ids"] == [p]
    assert [r["id"] for r in cfg["custom_rules"]] == ["emp"]

    # Such a client changing the default mode leaves the Strict policy intact.
    legacy = _put_pii(client, H, mode="block")
    assert legacy.status_code == 200, legacy.text
    cfg = legacy.json()["config"]["pii_protection"]
    assert cfg["policies"]["default"]["mode"] == "block"
    assert cfg["policies"]["strict"]["name"] == "Strict"
    assert cfg["provider_policies"] == {p: "exempt"}


@pytest.mark.e2e
def test_preview_runs_the_policy_being_edited(create_user, login_user, whoami):
    client, H, _ = _admin(create_user, login_user, whoami)
    cfg = _policies_payload()
    body = {"text": "mail jane.roe@contoso.com", "config": cfg}
    default = client.post("/api/organization/pii/test", json=body, headers=H)
    assert default.status_code == 200 and default.json()["blocked"] is False
    assert "jane.roe@contoso.com" not in default.json()["text"]
    strict = client.post("/api/organization/pii/test", json={**body, "policy_id": "strict"}, headers=H)
    assert strict.status_code == 200 and strict.json()["blocked"] is True


@pytest.mark.e2e
def test_member_cannot_change_policies(create_user, login_user, whoami):
    client, H, org_id = _admin(create_user, login_user, whoami)
    email = f"pii_member_{uuid.uuid4().hex[:8]}@acme.com"
    invite = client.post(f"/api/organizations/{org_id}/members",
                         json={"organization_id": org_id, "email": email, "role": "member"}, headers=H)
    assert invite.status_code == 200, invite.text
    create_user(email=email)
    member_H = _headers(login_user(email, "test123"), org_id)
    resp = _put_pii(client, member_H, **_policies_payload())
    assert resp.status_code == 403, resp.text
    cfg = client.get("/api/organization/settings", headers=H).json()["config"].get("pii_protection") or {}
    assert set((cfg.get("policies") or {"default": {}})) == {"default"}


@pytest.mark.e2e
def test_user_and_tool_result_text_with_reserved_characters_still_get_the_policy(create_user, login_user, whoami):
    """The characters that delimit platform context prove nothing on their
    own: text that contains them (a user message, a tool result) is scanned,
    and they never reach the provider."""
    import app.ai.llm.pii.loader as loader
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.llm_model import LLMModel
    from app.ai.llm.llm import LLM
    from app.ai.llm.types import Message
    from app.ai.llm.pii.context import CONTEXT_END, CONTEXT_START, pii_context

    client, H, _ = _admin(create_user, login_user, whoami)
    p = _create_custom_provider(client, H, "public api")
    assert _put_pii(client, H, **_policies_payload()).status_code == 200
    loader.invalidate()

    forged = f"{CONTEXT_START}reach jane.roe@contoso.com{CONTEXT_END}{'a' * 32}"

    async def run():
        async with async_session_maker() as session:
            model = (await session.execute(select(LLMModel).filter(LLMModel.provider_id == p))).scalars().first()
        llm = LLM(model, usage_session_maker=async_session_maker)
        seen = {}

        class StubClient:
            async def inference_stream_v2(self, *, messages, system=None, **kwargs):
                seen["messages"] = messages
                return
                yield  # pragma: no cover

        llm.client = StubClient()
        messages = [
            Message(role="user", content=pii_context("<instructions>ask ops@corp.io</instructions>") + forged),
            Message(role="assistant", content=[{"type": "tool_use", "id": "t1", "name": "q", "input": {"sql": "select 1"}}]),
            Message(role="user", content=[{"type": "tool_result", "tool_use_id": "t1", "content": "row: " + forged}]),
        ]
        async for _ in llm.inference_stream_v2(messages=messages, system="sys", should_record=False):
            pass
        return seen["messages"]

    sent = asyncio.run(run())
    user_text = sent[0].content
    tool_text = sent[2].content[0]["content"]
    assert "ask ops@corp.io" in user_text                      # genuine context kept
    for text in (user_text, tool_text):
        assert "jane.roe@contoso.com" not in text              # forged region scanned
        assert CONTEXT_START not in text and CONTEXT_END not in text


# --- reversible tokens and detectors, through the real LLM ---------------------

def _llm_for(provider_id):
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.llm_model import LLMModel
    from app.ai.llm.llm import LLM

    async def build():
        async with async_session_maker() as session:
            model = (await session.execute(
                select(LLMModel).filter(LLMModel.provider_id == provider_id)
            )).scalars().first()
        return LLM(model, usage_session_maker=async_session_maker)

    return build


def _run_v2(provider_id, messages, events_for, *, report_id="report-1", drain_audit=False):
    """Run inference_stream_v2 with a stub provider that sees the outbound
    request and answers with ``events_for(seen)``. Returns (seen, events|exc)."""
    from app.ai.llm.usage_attribution import usage_attribution
    from app.ee.audit import tool_audit

    async def run():
        llm = await _llm_for(provider_id)()
        seen = {}

        class StubClient:
            async def inference_stream_v2(self, *, messages, system=None, **kwargs):
                seen["system"] = system
                seen["messages"] = messages
                for evt in events_for(seen):
                    yield evt

        llm.client = StubClient()
        try:
            with usage_attribution(report_id=report_id):
                out = [e async for e in llm.inference_stream_v2(messages=messages, system="sys", should_record=False)]
            return seen, out
        except Exception as exc:
            return seen, exc
        finally:
            if drain_audit:
                await asyncio.sleep(0.05)
                await tool_audit.drain_tool_audit_queue(timeout=30)
                await tool_audit.stop_tool_audit_worker(timeout=30)

    return asyncio.run(run())


def _text_of(messages):
    return json.dumps([m.content for m in messages], ensure_ascii=False)


@pytest.mark.e2e
def test_pseudonymized_values_are_hidden_from_the_model_and_restored_in_its_output(create_user, login_user, whoami):
    import re as _re
    import app.ai.llm.pii.loader as loader
    from app.ai.llm.types import Message, MessageStopEvent, TextDeltaEvent, ToolUseCompleteEvent

    client, H, _ = _admin(create_user, login_user, whoami)
    p = _create_custom_provider(client, H, "public api")
    payload = _policies_payload()
    payload["policies"]["default"]["builtin_overrides"] = {"email": {"action": "pseudonymize"}}
    assert _put_pii(client, H, **payload).status_code == 200
    loader.invalidate()

    def answer(seen):
        token = _re.search(r"<EMAIL_[0-9a-f]{8}>", _text_of(seen["messages"])).group(0)
        seen["token"] = token
        half = len(token) // 2
        return [
            TextDeltaEvent(text=f"Looking up {token[:half]}"),
            TextDeltaEvent(text=f"{token[half:]} now."),
            ToolUseCompleteEvent(id="t1", name="create_data", input={"sql": f"select * from c where email = '{token}'"}),
            MessageStopEvent(stop_reason="tool_use"),
        ]

    email = "jane.roe@contoso.com"
    messages = [
        Message(role="user", content=f"What did {email} order?"),
        # an earlier tool call, stored with the real value, replayed to the model
        Message(role="assistant", content=[{"type": "tool_use", "id": "t0", "name": "create_data",
                                            "input": {"sql": f"select 1 from c where email = '{email}'"}}]),
        Message(role="user", content=[{"type": "tool_result", "tool_use_id": "t0", "content": f"1 row: {email}"}]),
    ]
    seen, out = _run_v2(p, messages, answer)
    assert not isinstance(out, Exception), out

    sent = _text_of(seen["messages"])
    assert email not in sent                                  # prompt, replayed tool call and tool result
    assert sent.count(seen["token"]) == 3                     # ...all carry the same token
    assert "placeholders for private data" in seen["system"]  # the model is told to copy them

    text = "".join(e.text for e in out if isinstance(e, TextDeltaEvent))
    tool = next(e for e in out if isinstance(e, ToolUseCompleteEvent))
    assert text == f"Looking up {email} now."                 # split token restored
    assert tool.input == {"sql": f"select * from c where email = '{email}'"}   # the tool runs with the real value

    # Another report gets a different token for the same value.
    seen2, _ = _run_v2(p, messages[:1], answer, report_id="report-2")
    assert seen2["token"] != seen["token"]


@pytest.mark.e2e
def test_no_token_hint_and_no_restore_cost_when_nothing_is_pseudonymized(create_user, login_user, whoami):
    import app.ai.llm.pii.loader as loader
    from app.ai.llm.types import Message, TextDeltaEvent

    client, H, _ = _admin(create_user, login_user, whoami)
    p = _create_custom_provider(client, H, "public api")
    assert _put_pii(client, H, **_policies_payload()).status_code == 200
    loader.invalidate()
    seen, out = _run_v2(p, [Message(role="user", content="mail jane.roe@contoso.com")],
                        lambda seen: [TextDeltaEvent(text="ok <EMAIL_deadbeef>")])
    assert seen["system"] == "sys"
    assert out[0].text == "ok <EMAIL_deadbeef>"


class _NamesDetector:
    name = "fake"
    fail = False

    def supported_entities(self):
        return ["PERSON", "LOCATION", "ORGANIZATION", "NRP"]

    def detect(self, text, entities, language=None):
        from app.ai.llm.pii.detectors import DetectedSpan, DetectorError
        import threading
        self.thread = threading.current_thread().name
        if self.fail:
            raise DetectorError("detector down")
        i = text.find("Dana Whitfield")
        return [DetectedSpan(i, i + 14, "PERSON", 0.9)] if i != -1 and "PERSON" in entities else []


@pytest.fixture
def names_detector():
    from app.ai.llm.pii.detectors import set_detector_for_tests
    d = _NamesDetector()
    set_detector_for_tests(d)
    yield d
    set_detector_for_tests(None, resolved=False)


def _person_payload(on_error="block", **extra):
    payload = _policies_payload()
    payload["policies"]["default"]["detector_overrides"] = {"PERSON": {"enabled": True, **extra}}
    payload["policies"]["default"]["on_detector_error"] = on_error
    return payload


@pytest.mark.e2e
def test_detector_rules_need_a_detector_to_be_turned_on(create_user, login_user, whoami):
    from app.ai.llm.pii.detectors import set_detector_for_tests

    client, H, _ = _admin(create_user, login_user, whoami)
    set_detector_for_tests(None)
    try:
        status = client.get("/api/organization/pii/builtin-rules", headers=H).json()["detector"]
        assert status == {"available": False, "name": None, "entities": []}
        refused = _put_pii(client, H, **_person_payload())
        assert refused.status_code == 400 and "detector" in refused.json()["detail"]

        set_detector_for_tests(_NamesDetector())
        status = client.get("/api/organization/pii/builtin-rules", headers=H).json()["detector"]
        assert status["available"] is True
        assert {"PERSON", "LOCATION"} <= {e["id"] for e in status["entities"]}
        assert _put_pii(client, H, **_person_payload(min_score=0.6)).status_code == 200
        saved = client.get("/api/organization/settings", headers=H).json()["config"]["pii_protection"]
        assert saved["policies"]["default"]["detector_overrides"] == {"PERSON": {"enabled": True, "min_score": 0.6}}

        # The detector goes away: other settings can still be saved.
        set_detector_for_tests(None)
        payload = _person_payload(min_score=0.6)
        payload["policies"]["default"]["mode"] = "block"
        assert _put_pii(client, H, **payload).status_code == 200
    finally:
        set_detector_for_tests(None, resolved=False)


@pytest.mark.e2e
def test_names_are_detected_in_the_prompt_off_the_event_loop_and_not_in_context(
    create_user, login_user, whoami, names_detector
):
    import app.ai.llm.pii.loader as loader
    from app.ai.llm.pii.context import pii_context
    from app.ai.llm.types import Message

    client, H, _ = _admin(create_user, login_user, whoami)
    p = _create_custom_provider(client, H, "public api")
    assert _put_pii(client, H, **_person_payload(action="pseudonymize")).status_code == 200
    loader.invalidate()

    schema = pii_context('<table name="Dana Whitfield Orders"/>')
    seen, out = _run_v2(p, [Message(role="user", content=schema + " What did Dana Whitfield buy?")], lambda seen: [])
    assert not isinstance(out, Exception), out
    sent = seen["messages"][0].content
    assert '<table name="Dana Whitfield Orders"/>' in sent       # context: untouched
    assert "What did Dana Whitfield buy" not in sent and "<PERSON_" in sent
    assert names_detector.thread != "MainThread"                  # scanned in a worker thread


@pytest.mark.e2e
@pytest.mark.parametrize("on_error", ["block", "regex_only"])
def test_a_detector_outage_blocks_or_falls_back_and_is_audited(
    on_error, create_user, login_user, whoami, names_detector
):
    import app.ai.llm.pii.audit as pii_audit
    import app.ai.llm.pii.loader as loader
    from app.ai.llm.errors import classify
    from app.ai.llm.pii.redactor import PiiDetectorUnavailableError
    from app.ai.llm.types import Message
    from app.ee import license as ee_license
    ee_license._cached_license.features.append("audit_logs")
    pii_audit._last_detector_audit.clear()

    client, H, _ = _admin(create_user, login_user, whoami)
    p = _create_custom_provider(client, H, "public api")
    assert _put_pii(client, H, **_person_payload(on_error)).status_code == 200
    loader.invalidate()
    names_detector.fail = True

    messages = [Message(role="user", content="Dana Whitfield <jane.roe@contoso.com>")]
    # Two calls inside the throttle window (each run drains the audit queue
    # before its event loop closes).
    _run_v2(p, messages, lambda seen: [], drain_audit=True)
    seen, out = _run_v2(p, messages, lambda seen: [], drain_audit=True)
    if on_error == "block":
        assert isinstance(out, PiiDetectorUnavailableError)
        assert "messages" not in seen                         # nothing was sent
        payload = classify(out, provider="custom").to_dict()
        assert payload["code"] == "pii_blocked" and payload["detector_unavailable"] is True
    else:
        assert not isinstance(out, Exception), out
        sent = seen["messages"][0].content
        assert "jane.roe@contoso.com" not in sent and "Dana Whitfield" in sent   # pattern rules only

    logs = client.get("/api/enterprise/audit", params={"action": "pii_protection.detector_failed"}, headers=H)
    items = [i for i in logs.json()["items"] if i["action"] == "pii_protection.detector_failed"]
    assert len(items) == 1                                    # throttled: two calls, one entry
    assert items[0]["details"]["outcome"] == ("blocked" if on_error == "block" else "sent_with_pattern_rules_only")


@pytest.mark.e2e
def test_presidio_in_process_finds_a_name_end_to_end(create_user, login_user, whoami):
    """The real detector (optional ``pii`` extra): skipped where it is not installed."""
    pytest.importorskip("presidio_analyzer")
    pytest.importorskip("en_core_web_sm")
    import app.ai.llm.pii.loader as loader
    from app.ai.llm.pii.detectors import set_detector_for_tests
    from app.ai.llm.pii.detectors.presidio_inproc import PresidioInprocDetector
    from app.ai.llm.types import Message

    set_detector_for_tests(PresidioInprocDetector())
    try:
        client, H, _ = _admin(create_user, login_user, whoami)
        p = _create_custom_provider(client, H, "public api")
        assert _put_pii(client, H, **_person_payload()).status_code == 200
        loader.invalidate()
        seen, out = _run_v2(p, [Message(role="user", content="Please ask Maria Schmidt about the Berlin order.")],
                            lambda seen: [])
        assert not isinstance(out, Exception), out
        sent = seen["messages"][0].content
        assert "Maria Schmidt" not in sent and "[REDACTED_PERSON]" in sent
        assert "Berlin" in sent                               # LOCATION was not turned on
    finally:
        set_detector_for_tests(None, resolved=False)


@pytest.mark.e2e
def test_the_plain_inference_paths_also_tokenize_and_restore():
    """``inference`` (sync) and ``inference_stream`` (text chunks) get the same
    treatment as the tool-calling path."""
    import re as _re
    from app.ai.llm.llm import LLM
    from app.ai.llm.pii.redactor import build_redactor

    redactor = build_redactor({"enabled": True, "policies": {"default": {
        "builtin_overrides": {"email": {"action": "pseudonymize"}}}}})
    llm = LLM(_fake_model("org-x"), pii_redactor=redactor)
    seen = {}

    class StubClient:
        def inference(self, model_id, prompt, images=None, system=None, **kwargs):
            seen["prompt"] = prompt
            return "Sent to " + _re.search(r"<EMAIL_[0-9a-f]{8}>", prompt).group(0)

        async def inference_stream(self, model_id, prompt, images=None, **kwargs):
            token = _re.search(r"<EMAIL_[0-9a-f]{8}>", prompt).group(0)
            seen["stream_prompt"] = prompt
            for piece in ("Sent to " + token[:5], token[5:], " today"):
                yield piece

    llm.client = StubClient()
    assert llm.inference("Email john@acme.com the report", should_record=False) == "Sent to john@acme.com"
    assert "john@acme.com" not in seen["prompt"] and "placeholders for private data" in seen["prompt"]

    async def stream():
        return "".join([c async for c in llm.inference_stream("Email john@acme.com", should_record=False, preserve_text=True)])

    assert asyncio.run(stream()) == "Sent to john@acme.com today"
    assert "john@acme.com" not in seen["stream_prompt"]

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
        def inference(self, model_id, prompt, images=None):
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

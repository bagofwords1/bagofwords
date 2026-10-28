"""Embedded chat (Settings → Embedded chat, /api/embed/*).

The contract under test, end to end through the HTTP surface:

* Administration is manage_settings-only; the signing private key is readable
  exactly once (create / rotate-key); edits are revision-checked.
* A trusted host assertion (RS256, iss/aud/iat/exp/jti/sub) signed with the
  embed's key and naming a member through the admin-set attribute becomes an
  embed session. Anything else — wrong key, wrong issuer or audience, expired,
  over-long, replayed, algorithm confusion, unmapped or ambiguous subject,
  disabled embed — is refused with a typed error and no session.
* An embed session works only on its own embed's /api/embed routes: never on
  the main API, never on another embed, never after logout, key rotation,
  session revocation or disabling.
* Chat runs on a hidden per-(embed, user) thread carrying the embed's runtime
  (roster ∩ the user's access, forced-off tools, model); threads are invisible
  in the user's report list and isolated per user and per embed; a repeated
  send with the same idempotency key does not start a second run.

The agent loop is stubbed at AgentV2.main_execution (no LLM); routes,
services and the database are real.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid

import jwt
import pytest

from app.ai.agent_v2 import AgentV2
from app.services.embed_service import FORCED_OFF_TOOLS, generate_keypair


def _h(token, org_id=None):
    headers = {"Authorization": f"Bearer {token}"}
    if org_id:
        headers["X-Organization-Id"] = str(org_id)
    return headers


def _stub_agent(monkeypatch):
    async def fake_main_execution(self):
        self.system_completion.status = "success"
        self.system_completion.completion = {"content": "stubbed answer"}
        self.db.add(self.system_completion)
        await self.db.commit()
    monkeypatch.setattr(AgentV2, "main_execution", fake_main_execution)


def sign(private_key, *, iss="erp", aud, sub, jti=None, iat=None, ttl=300, algorithm="RS256", key=None, **extra):
    now = int(time.time())
    claims = {"iss": iss, "aud": aud, "sub": sub, "iat": now if iat is None else iat, "jti": jti or uuid.uuid4().hex}
    claims["exp"] = claims["iat"] + ttl
    claims.update(extra)
    return jwt.encode(claims, key if key is not None else private_key, algorithm=algorithm)


def forge_hs256(claims: dict, key: bytes) -> str:
    """An HS256 token signed with arbitrary bytes (PyJWT refuses to HMAC with a
    PEM, which is exactly the confusion attack this simulates)."""
    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    signing_input = f"{b64(json.dumps({'alg': 'HS256', 'typ': 'JWT'}).encode())}.{b64(json.dumps(claims).encode())}"
    sig = hmac.new(key, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{b64(sig)}"


def _thread_row(report_id: str) -> dict:
    """Read the hidden thread straight from the test database (sync engine, the
    way tests/fixtures/user.py reads invite tokens): there is deliberately no
    API that returns an embed thread as a report."""
    import os
    from sqlalchemy import create_engine, text

    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        from app.settings.config import settings as _s
        url = getattr(_s, "TEST_DATABASE_URL", None)
    sync_url = url.replace("sqlite+aiosqlite:", "sqlite:").replace("postgresql+asyncpg:", "postgresql:")
    engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            report = conn.execute(text(
                "SELECT report_type, embed_id, user_id, tools, model_id, follow_ups FROM reports WHERE id = :id"
            ), {"id": report_id}).mappings().one()
            roster = conn.execute(text(
                "SELECT data_source_id FROM report_data_source_association WHERE report_id = :id"
            ), {"id": report_id}).scalars().all()
    finally:
        engine.dispose()
    tools = report["tools"]
    if isinstance(tools, str):
        tools = json.loads(tools)
    return {"type": report["report_type"], "embed_id": report["embed_id"], "user_id": report["user_id"],
            "tools": dict(tools or {}), "model_id": report["model_id"], "follow_ups": report["follow_ups"],
            "roster": sorted(str(x) for x in roster)}


def _drain(response):
    """Consume an SSE response body so the stubbed run completes."""
    return b"".join(response.iter_bytes()) if hasattr(response, "iter_bytes") else response.content


class World:
    """One org, an admin and a mapped member, a default model, one embed."""

    def __init__(self, test_client, create_user, login_user, whoami, create_data_source):
        self.client = test_client
        self.create_user, self.login_user, self.whoami = create_user, login_user, whoami
        self.create_data_source = create_data_source

        admin_email = f"embed_admin_{uuid.uuid4().hex[:8]}@test.com"
        create_user(email=admin_email, password="test123")
        self.admin_token = login_user(admin_email, "test123")
        me = whoami(self.admin_token)
        self.admin_id = me["id"]
        self.org_id = me["organizations"][0]["id"]
        self.admin_email = admin_email

        r = test_client.post("/api/llm/providers", headers=_h(self.admin_token, self.org_id), json={
            "name": f"openai-{uuid.uuid4().hex[:6]}", "provider_type": "openai",
            "credentials": {"api_key": "sk-test-not-used"},
            "models": [{"model_id": "gpt-6-luna", "name": "GPT-6 Luna", "is_custom": False, "is_default": True}],
        })
        assert r.status_code == 200, r.text
        self.model_id = next(m["id"] for m in test_client.get(
            "/api/llm/models", headers=_h(self.admin_token, self.org_id)).json() if m["model_id"] == "gpt-6-luna")

    # ── members ──────────────────────────────────────────────────────
    def invite_member(self, *, attributes=None, role="member"):
        email = f"embed_member_{uuid.uuid4().hex[:8]}@test.com"
        r = self.client.post(f"/api/organizations/{self.org_id}/members",
                             json={"organization_id": self.org_id, "email": email, "role": role},
                             headers=_h(self.admin_token, self.org_id))
        assert r.status_code == 200, r.text
        membership_id = r.json()["id"]
        self.create_user(email=email, password="Test1234!")
        token = self.login_user(email, "Test1234!")
        info = self.whoami(token)
        if attributes is not None:
            self.set_attributes(membership_id, attributes)
        return {"email": email, "token": token, "user_id": info["id"], "membership_id": membership_id}

    def set_attributes(self, membership_id, attributes):
        r = self.client.put(f"/api/organizations/{self.org_id}/members/{membership_id}",
                            json={"custom_attributes": attributes}, headers=_h(self.admin_token, self.org_id))
        assert r.status_code == 200, r.text

    def my_membership_id(self, token):
        members = self.client.get(f"/api/organizations/{self.org_id}/members", headers=_h(token, self.org_id)).json()
        uid = self.whoami(token)["id"]
        return next(m["id"] for m in members if m.get("user_id") == uid)

    # ── embeds ───────────────────────────────────────────────────────
    def create_embed(self, **overrides):
        body = {
            "name": "ERP assistant",
            "allowed_origins": ["https://erp.example.com"],
            "issuer": "erp",
            "subject_attribute": "employee_id",
            "runtime": {"data_source_ids": [], "model_id": self.model_id},
            "branding": {"display_name": "Ask BOW", "accent_color": "#0ea5e9", "greeting": "Hi"},
        }
        body.update(overrides)
        r = self.client.post("/api/embeds", json=body, headers=_h(self.admin_token, self.org_id))
        assert r.status_code == 200, r.text
        return r.json()

    def session(self, embed, assertion):
        return self.client.post(f"/api/embed/{embed['public_id']}/session", json={"assertion": assertion})

    def login_embed(self, embed, private_key, sub):
        r = self.session(embed, sign(private_key, aud=embed["public_id"], sub=sub))
        assert r.status_code == 200, r.text
        return r.json()["token"]

    def chat(self, embed, token, content="hello", **extra):
        with self.client.stream("POST", f"/api/embed/{embed['public_id']}/chat/completions",
                                json={"content": content, **extra}, headers=_h(token)) as r:
            body = b"".join(r.iter_bytes())
            return r.status_code, body

    def history(self, embed, token):
        r = self.client.get(f"/api/embed/{embed['public_id']}/chat/completions", headers=_h(token))
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture
def world(test_client, create_user, login_user, whoami, create_data_source):
    return World(test_client, create_user, login_user, whoami, create_data_source)


@pytest.fixture
def allow_multiple_orgs():
    """The rare second-organization scenario (same pattern as test_oauth_mcp_multiorg)."""
    from app.settings.config import settings as bow_settings
    flags = bow_settings.bow_config.features
    saved = (flags.allow_multiple_organizations, flags.allow_uninvited_signups)
    flags.allow_multiple_organizations = True
    flags.allow_uninvited_signups = True
    try:
        yield
    finally:
        flags.allow_multiple_organizations, flags.allow_uninvited_signups = saved


# ─────────────────────────────────────────────────────────────────────────
# Administration
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_private_key_is_returned_once_and_never_again(world):
    created = world.create_embed()
    assert created["private_key"].startswith("-----BEGIN PRIVATE KEY-----")
    assert created["public_key"].startswith("-----BEGIN PUBLIC KEY-----")
    assert created["key_fingerprint"] and created["public_id"].startswith("emb_")

    fetched = world.client.get(f"/api/embeds/{created['id']}", headers=_h(world.admin_token, world.org_id)).json()
    listed = world.client.get("/api/embeds", headers=_h(world.admin_token, world.org_id)).json()
    for payload in (fetched, *listed):
        assert "private_key" not in payload and "public_key" not in payload
    assert fetched["key_fingerprint"] == created["key_fingerprint"]


@pytest.mark.e2e
def test_admin_surface_requires_manage_settings(world):
    member = world.invite_member()
    embed = world.create_embed()
    denied = [
        world.client.get("/api/embeds", headers=_h(member["token"], world.org_id)),
        world.client.post("/api/embeds", json={"name": "x", "issuer": "i", "subject_attribute": "k"},
                          headers=_h(member["token"], world.org_id)),
        world.client.put(f"/api/embeds/{embed['id']}", json={"revision": embed["revision"], "name": "y"},
                         headers=_h(member["token"], world.org_id)),
        world.client.post(f"/api/embeds/{embed['id']}/rotate-key", headers=_h(member["token"], world.org_id)),
        world.client.delete(f"/api/embeds/{embed['id']}", headers=_h(member["token"], world.org_id)),
        world.client.post(f"/api/embed/{embed['public_id']}/session/preview", headers=_h(member["token"], world.org_id)),
    ]
    assert all(r.status_code == 403 for r in denied), [r.status_code for r in denied]


@pytest.mark.e2e
def test_update_is_revision_checked_and_validated(world):
    embed = world.create_embed()
    stale = world.client.put(f"/api/embeds/{embed['id']}", json={"revision": embed["revision"] + 5, "name": "late"},
                             headers=_h(world.admin_token, world.org_id))
    assert stale.status_code == 409 and stale.json()["error_code"] == "embed.revision_conflict"

    ok = world.client.put(f"/api/embeds/{embed['id']}", json={
        "revision": embed["revision"], "name": "Renamed",
        "allowed_origins": ["https://ERP.example.com/", "https://portal.example.com:8443"],
        "runtime": {"data_source_ids": [], "tools": {"web_search": False}},
    }, headers=_h(world.admin_token, world.org_id))
    assert ok.status_code == 200, ok.text
    assert ok.json()["revision"] == embed["revision"] + 1
    assert ok.json()["allowed_origins"] == ["https://erp.example.com", "https://portal.example.com:8443"]

    bad_cases = [
        {"allowed_origins": ["https://erp.example.com/path"]},
        {"allowed_origins": ["erp.example.com"]},
        {"runtime": {"data_source_ids": [str(uuid.uuid4())]}},
        {"runtime": {"model_id": str(uuid.uuid4())}},
        {"runtime": {"tools": {"not_a_tool": False}}},
        {"branding": {"accent_color": "red"}},
        {"subject_attribute": "bad key!"},
    ]
    for case in bad_cases:
        r = world.client.put(f"/api/embeds/{embed['id']}", json={"revision": ok.json()["revision"], **case},
                             headers=_h(world.admin_token, world.org_id))
        assert r.status_code in (400, 422), (case, r.text)


@pytest.mark.e2e
def test_bootstrap_is_public_and_appearance_only(world):
    embed = world.create_embed()
    r = world.client.get(f"/api/embed/{embed['public_id']}/bootstrap")
    assert r.status_code == 200
    body = r.json()
    assert body["branding"]["display_name"] == "Ask BOW"
    assert body["allowed_origins"] == ["https://erp.example.com"]
    for forbidden in ("runtime", "agents", "members", "public_key", "issuer", "subject_attribute"):
        assert forbidden not in body
    assert world.client.get(f"/api/embed/emb_{uuid.uuid4().hex[:20]}/bootstrap").status_code == 404
    sdk = world.client.get("/api/embed/sdk/v1/bow-embed.js")
    assert sdk.status_code == 200 and "javascript" in sdk.headers["content-type"]


# ─────────────────────────────────────────────────────────────────────────
# Authentication matrix
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_valid_assertion_yields_a_session_for_the_mapped_member(world):
    embed = world.create_embed()
    member = world.invite_member(attributes={"employee_id": "E-1042"})
    r = world.session(embed, sign(embed["private_key"], aud=embed["public_id"], sub="E-1042"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"].startswith("bow_embed_")
    assert body["user"]["email"] == member["email"]
    assert body["embed_id"] == embed["public_id"]
    assert body["agents"] == []  # empty admin roster stays empty


@pytest.mark.e2e
@pytest.mark.parametrize("case", [
    "wrong_key", "wrong_issuer", "wrong_audience", "expired", "too_long_lived", "future_iat",
    "missing_jti", "missing_sub", "hs256_with_public_key", "alg_none", "garbage",
])
def test_bad_assertions_are_refused(world, case):
    embed = world.create_embed()
    world.invite_member(attributes={"employee_id": "E-7"})
    pk, aud = embed["private_key"], embed["public_id"]
    other_private, _, _ = generate_keypair()
    now = int(time.time())
    assertion = {
        "wrong_key": lambda: sign(other_private, aud=aud, sub="E-7"),
        "wrong_issuer": lambda: sign(pk, iss="someone-else", aud=aud, sub="E-7"),
        "wrong_audience": lambda: sign(pk, aud="emb_other", sub="E-7"),
        "expired": lambda: sign(pk, aud=aud, sub="E-7", iat=now - 1000, ttl=300),
        "too_long_lived": lambda: sign(pk, aud=aud, sub="E-7", ttl=3600 * 8),
        "future_iat": lambda: sign(pk, aud=aud, sub="E-7", iat=now + 900, ttl=300),
        "missing_jti": lambda: jwt.encode({"iss": "erp", "aud": aud, "sub": "E-7", "iat": now, "exp": now + 300}, pk, algorithm="RS256"),
        "missing_sub": lambda: jwt.encode({"iss": "erp", "aud": aud, "jti": "j", "iat": now, "exp": now + 300}, pk, algorithm="RS256"),
        "hs256_with_public_key": lambda: forge_hs256(
            {"iss": "erp", "aud": aud, "sub": "E-7", "jti": "j", "iat": now, "exp": now + 300},
            embed["public_key"].encode()),
        "alg_none": lambda: jwt.encode({"iss": "erp", "aud": aud, "sub": "E-7", "jti": "j", "iat": now, "exp": now + 300}, None, algorithm="none"),
        "garbage": lambda: "not.a.jwt-at-all-" + "x" * 40,
    }[case]()
    r = world.session(embed, assertion)
    assert r.status_code == 401, (case, r.text)
    assert r.json()["error_code"] == "embed.assertion_invalid", (case, r.text)
    assert "token" not in r.json()


@pytest.mark.e2e
def test_assertion_is_single_use(world):
    embed = world.create_embed()
    world.invite_member(attributes={"employee_id": "E-9"})
    assertion = sign(embed["private_key"], aud=embed["public_id"], sub="E-9")
    assert world.session(embed, assertion).status_code == 200
    replay = world.session(embed, assertion)
    assert replay.status_code == 401 and replay.json()["error_code"] == "embed.assertion_replayed"


@pytest.mark.e2e
def test_identity_must_map_to_exactly_one_member_by_admin_attribute(world):
    embed = world.create_embed()
    a = world.invite_member(attributes={"employee_id": "E-dup"})
    b = world.invite_member(attributes={"employee_id": "E-dup"})
    world.invite_member(attributes={"other_key": "E-unmapped"})

    unmapped = world.session(embed, sign(embed["private_key"], aud=embed["public_id"], sub="E-unmapped"))
    assert unmapped.status_code == 401 and unmapped.json()["error_code"] == "embed.identity_unmapped"

    # Email is never an identity: a sub equal to a member's email maps nothing.
    by_email = world.session(embed, sign(embed["private_key"], aud=embed["public_id"], sub=a["email"]))
    assert by_email.status_code == 401 and by_email.json()["error_code"] == "embed.identity_unmapped"

    ambiguous = world.session(embed, sign(embed["private_key"], aud=embed["public_id"], sub="E-dup"))
    assert ambiguous.status_code == 401 and ambiguous.json()["error_code"] == "embed.identity_ambiguous"

    # Resolve the collision: exactly one member keeps the value → that one signs in.
    world.set_attributes(b["membership_id"], {})
    ok = world.session(embed, sign(embed["private_key"], aud=embed["public_id"], sub="E-dup"))
    assert ok.status_code == 200 and ok.json()["user"]["email"] == a["email"]


@pytest.mark.e2e
def test_a_member_of_another_organization_is_not_mapped(world, test_client, create_organization, allow_multiple_orgs):
    embed = world.create_embed()
    # Same attribute key and value, different organization (the admin's second
    # org): must not match an embed of the first.
    other_org = create_organization(name=f"Other {uuid.uuid4().hex[:4]}", user_token=world.admin_token)
    members = test_client.get(f"/api/organizations/{other_org}/members", headers=_h(world.admin_token, other_org)).json()
    r = test_client.put(f"/api/organizations/{other_org}/members/{members[0]['id']}",
                        json={"custom_attributes": {"employee_id": "E-cross"}}, headers=_h(world.admin_token, other_org))
    assert r.status_code == 200, r.text
    refused = world.session(embed, sign(embed["private_key"], aud=embed["public_id"], sub="E-cross"))
    assert refused.status_code == 401 and refused.json()["error_code"] == "embed.identity_unmapped"


@pytest.mark.e2e
def test_disabled_embed_refuses_sign_in(world):
    embed = world.create_embed(enabled=False)
    world.invite_member(attributes={"employee_id": "E-off"})
    r = world.session(embed, sign(embed["private_key"], aud=embed["public_id"], sub="E-off"))
    assert r.status_code == 403 and r.json()["error_code"] == "embed.disabled"


# ─────────────────────────────────────────────────────────────────────────
# Sessions
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_embed_session_is_useless_outside_its_embed(world):
    embed = world.create_embed()
    other = world.create_embed(name="Other app")
    world.invite_member(attributes={"employee_id": "E-1"})
    token = world.login_embed(embed, embed["private_key"], "E-1")

    # Not a main-application credential.
    for path in ("/api/reports", "/api/users/whoami", "/api/organizations", "/api/embeds", "/api/data_sources"):
        r = world.client.get(path, headers=_h(token, world.org_id))
        assert r.status_code in (401, 403), (path, r.status_code)

    # Not valid on another embed, even in the same organization.
    r = world.client.get(f"/api/embed/{other['public_id']}/session", headers=_h(token))
    assert r.status_code == 401 and r.json()["error_code"] == "embed.session_invalid"
    status, _ = world.chat(other, token)
    assert status == 401

    # Valid on its own.
    r = world.client.get(f"/api/embed/{embed['public_id']}/session", headers=_h(token))
    assert r.status_code == 200 and r.json()["embed_id"] == embed["public_id"]


@pytest.mark.e2e
def test_logout_rotation_revocation_and_disable_all_end_sessions(world):
    embed = world.create_embed()
    world.invite_member(attributes={"employee_id": "E-1"})
    status_url = f"/api/embed/{embed['public_id']}/session"

    # logout
    t1 = world.login_embed(embed, embed["private_key"], "E-1")
    assert world.client.delete(status_url, headers=_h(t1)).status_code == 204
    assert world.client.get(status_url, headers=_h(t1)).status_code == 401

    # revoke all sessions
    t2 = world.login_embed(embed, embed["private_key"], "E-1")
    r = world.client.post(f"/api/embeds/{embed['id']}/revoke-sessions", headers=_h(world.admin_token, world.org_id))
    assert r.status_code == 200 and r.json()["active_sessions"] == 0
    assert world.client.get(status_url, headers=_h(t2)).status_code == 401

    # key rotation: old sessions AND old-key assertions die, new key works
    t3 = world.login_embed(embed, embed["private_key"], "E-1")
    rotated = world.client.post(f"/api/embeds/{embed['id']}/rotate-key", headers=_h(world.admin_token, world.org_id))
    assert rotated.status_code == 200 and rotated.json()["key_fingerprint"] != embed["key_fingerprint"]
    assert world.client.get(status_url, headers=_h(t3)).status_code == 401
    old_key = world.session(embed, sign(embed["private_key"], aud=embed["public_id"], sub="E-1"))
    assert old_key.status_code == 401
    t4 = world.login_embed(embed, rotated.json()["private_key"], "E-1")
    assert world.client.get(status_url, headers=_h(t4)).status_code == 200

    # disable
    current = world.client.get(f"/api/embeds/{embed['id']}", headers=_h(world.admin_token, world.org_id)).json()
    r = world.client.put(f"/api/embeds/{embed['id']}", json={"revision": current["revision"], "enabled": False},
                         headers=_h(world.admin_token, world.org_id))
    assert r.status_code == 200
    assert world.client.get(status_url, headers=_h(t4)).status_code == 401
    assert world.session(embed, sign(rotated.json()["private_key"], aud=embed["public_id"], sub="E-1")).status_code == 403


@pytest.mark.e2e
def test_admin_preview_session_is_the_admins_own_identity(world):
    embed = world.create_embed(enabled=False)
    r = world.client.post(f"/api/embed/{embed['public_id']}/session/preview", headers=_h(world.admin_token, world.org_id))
    assert r.status_code == 200, r.text
    assert r.json()["user"]["email"] == world.admin_email
    assert r.json()["token"].startswith("bow_embed_")


# ─────────────────────────────────────────────────────────────────────────
# Chat
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_chat_runs_on_a_hidden_isolated_thread_with_the_embed_runtime(world, monkeypatch, test_client):
    _stub_agent(monkeypatch)
    ds = world.create_data_source(name=f"Orders {uuid.uuid4().hex[:4]}", type="sqlite",
                                  config={"database": ":memory:"}, user_token=world.admin_token, org_id=world.org_id)
    embed = world.create_embed(runtime={"data_source_ids": [ds["id"]], "model_id": world.model_id,
                                        "tools": {"web_search": False}, "follow_ups": False})
    a = world.invite_member(attributes={"employee_id": "E-A"})
    b = world.invite_member(attributes={"employee_id": "E-B"})
    ta = world.login_embed(embed, embed["private_key"], "E-A")
    tb = world.login_embed(embed, embed["private_key"], "E-B")

    assert world.history(embed, ta) == {"report_id": None, "completions": [], "total_completions": 0}

    status, body = world.chat(embed, ta, "what are my open orders?", context={"recordId": "INC-1"},
                              metadata={"workflow_id": "wf-1"})
    assert status == 200, body[:400]
    assert b"completion.started" in body and b"completion.finished" in body

    mine = world.history(embed, ta)
    assert mine["total_completions"] >= 1 and mine["report_id"]
    prompts = [c for c in mine["completions"] if c.get("role") == "user"]
    assert prompts and prompts[0]["prompt"]["content"] == "what are my open orders?"
    # B has a different, empty thread; A's thread id never shows up for B.
    assert world.history(embed, tb) == {"report_id": None, "completions": [], "total_completions": 0}

    # The thread is invisible in the member's own report list.
    reports = test_client.get("/api/reports", headers=_h(a["token"], world.org_id)).json()
    listed_ids = {r["id"] for r in (reports if isinstance(reports, list) else reports.get("reports", []))}
    assert mine["report_id"] not in listed_ids

    # The thread carries the embed runtime: type, binding, roster, forced-off tools, model.
    row = _thread_row(mine["report_id"])
    assert row["type"] == "embed_chat" and row["embed_id"] == embed["id"] and row["user_id"] == a["user_id"]
    assert row["model_id"] == world.model_id and not row["follow_ups"]
    assert all(row["tools"].get(t) is False for t in FORCED_OFF_TOOLS)
    assert row["tools"].get("web_search") is False
    # The agent is private and the member holds no grant: the embed names it, the
    # member cannot reach it, so the thread gets nothing (never "everything").
    assert row["roster"] == []

    # Once the agent is public the SAME embed config yields it on the next turn
    # (runtime is re-applied per message; no redeploy, no new session).
    r = test_client.put(f"/api/data_sources/{ds['id']}", json={"is_public": True},
                        headers=_h(world.admin_token, world.org_id))
    assert r.status_code == 200, r.text
    status, _ = world.chat(embed, ta, "and now?")
    assert status == 200
    assert _thread_row(mine["report_id"])["roster"] == [ds["id"]]
    session = test_client.get(f"/api/embed/{embed['public_id']}/session", headers=_h(ta)).json()
    assert [a_["id"] for a_ in session["agents"]] == [ds["id"]]


@pytest.mark.e2e
def test_roster_is_admin_roster_intersected_with_the_users_own_access(world, monkeypatch):
    _stub_agent(monkeypatch)
    ds = world.create_data_source(name=f"Private {uuid.uuid4().hex[:4]}", type="sqlite",
                                  config={"database": ":memory:"}, user_token=world.admin_token, org_id=world.org_id)
    embed = world.create_embed(runtime={"data_source_ids": [ds["id"]], "model_id": world.model_id})
    world.invite_member(attributes={"employee_id": "E-M"})
    # The admin maps themselves too: same embed, two users, two different rosters.
    world.set_attributes(world.my_membership_id(world.admin_token), {"employee_id": "E-ADMIN"})
    member_token = world.login_embed(embed, embed["private_key"], "E-M")
    admin_token = world.login_embed(embed, embed["private_key"], "E-ADMIN")

    assert world.chat(embed, member_token, "hi")[0] == 200
    assert world.chat(embed, admin_token, "hi")[0] == 200
    member_roster = _thread_row(world.history(embed, member_token)["report_id"])["roster"]
    admin_roster = _thread_row(world.history(embed, admin_token)["report_id"])["roster"]
    assert member_roster == []          # named by the embed, not reachable by this member
    assert admin_roster == [ds["id"]]   # full admin reaches every agent → exactly the embed's roster
    # A roster never exceeds the embed's list, whoever the user is.
    assert set(member_roster) | set(admin_roster) <= {ds["id"]}


@pytest.mark.e2e
def test_agent_selection_is_refused_unless_allowed_and_within_roster(world, monkeypatch):
    _stub_agent(monkeypatch)
    ds = world.create_data_source(name=f"Pick {uuid.uuid4().hex[:4]}", type="sqlite",
                                  config={"database": ":memory:"}, user_token=world.admin_token, org_id=world.org_id)
    embed = world.create_embed(runtime={"data_source_ids": [ds["id"]], "model_id": world.model_id})
    world.invite_member(attributes={"employee_id": "E-S"})
    token = world.login_embed(embed, embed["private_key"], "E-S")
    status, body = world.chat(embed, token, "hi", data_source_ids=[ds["id"]])
    assert status == 403 and b"embed.agent_forbidden" in body

    current = world.client.get(f"/api/embeds/{embed['id']}", headers=_h(world.admin_token, world.org_id)).json()
    r = world.client.put(f"/api/embeds/{embed['id']}", headers=_h(world.admin_token, world.org_id), json={
        "revision": current["revision"],
        "runtime": {"data_source_ids": [ds["id"]], "model_id": world.model_id, "allow_agent_selection": True}})
    assert r.status_code == 200, r.text
    status, body = world.chat(embed, token, "hi", data_source_ids=[str(uuid.uuid4())])
    assert status == 403 and b"embed.agent_forbidden" in body


@pytest.mark.e2e
def test_oversized_context_and_metadata_are_rejected(world, monkeypatch):
    _stub_agent(monkeypatch)
    embed = world.create_embed()
    world.invite_member(attributes={"employee_id": "E-C"})
    token = world.login_embed(embed, embed["private_key"], "E-C")
    status, body = world.chat(embed, token, "hi", context={"blob": "x" * 5000})
    assert status == 400 and b"embed.context_too_large" in body
    status, body = world.chat(embed, token, "hi", metadata={"blob": "x" * 9000})
    assert status == 400
    status, _ = world.chat(embed, token, "   ")
    assert status in (400, 422)


@pytest.mark.e2e
def test_repeated_send_with_the_same_idempotency_key_runs_once(world, monkeypatch):
    _stub_agent(monkeypatch)
    embed = world.create_embed()
    world.invite_member(attributes={"employee_id": "E-I"})
    token = world.login_embed(embed, embed["private_key"], "E-I")
    key = uuid.uuid4().hex
    for _ in range(3):
        status, body = world.chat(embed, token, "same question", idempotency_key=key)
        assert status == 200, body[:300]
    history = world.history(embed, token)
    user_turns = [c for c in history["completions"] if c.get("role") == "user"]
    assert len(user_turns) == 1
    # A different key is a new turn.
    status, _ = world.chat(embed, token, "same question", idempotency_key=uuid.uuid4().hex)
    assert status == 200
    assert len([c for c in world.history(embed, token)["completions"] if c.get("role") == "user"]) == 2


@pytest.mark.e2e
def test_watch_and_cancel_are_bound_to_the_callers_own_thread(world, monkeypatch):
    _stub_agent(monkeypatch)
    embed = world.create_embed()
    world.invite_member(attributes={"employee_id": "E-W1"})
    world.invite_member(attributes={"employee_id": "E-W2"})
    t1 = world.login_embed(embed, embed["private_key"], "E-W1")
    t2 = world.login_embed(embed, embed["private_key"], "E-W2")
    status, _ = world.chat(embed, t1, "hi")
    assert status == 200
    system = next(c for c in world.history(embed, t1)["completions"] if c.get("role") != "user")
    base = f"/api/embed/{embed['public_id']}/chat/completions/{system['id']}"
    with world.client.stream("GET", f"{base}/stream", headers=_h(t1)) as r:
        assert r.status_code == 200
        b"".join(r.iter_bytes())
    with world.client.stream("GET", f"{base}/stream", headers=_h(t2)) as r:
        assert r.status_code == 404
    assert world.client.post(f"{base}/cancel", headers=_h(t2)).status_code == 404
    assert world.client.post(f"{base}/cancel", headers=_h(t1)).status_code == 200


@pytest.mark.e2e
def test_deleting_an_embed_removes_its_threads_and_sessions_only(world, monkeypatch):
    _stub_agent(monkeypatch)
    ds = world.create_data_source(name=f"Keep {uuid.uuid4().hex[:4]}", type="sqlite",
                                  config={"database": ":memory:"}, user_token=world.admin_token, org_id=world.org_id)
    embed = world.create_embed(runtime={"data_source_ids": [ds["id"]], "model_id": world.model_id})
    world.invite_member(attributes={"employee_id": "E-D"})
    token = world.login_embed(embed, embed["private_key"], "E-D")
    assert world.chat(embed, token, "hi")[0] == 200
    assert world.client.delete(f"/api/embeds/{embed['id']}", headers=_h(world.admin_token, world.org_id)).status_code == 204
    assert world.client.get(f"/api/embed/{embed['public_id']}/session", headers=_h(token)).status_code == 401
    assert world.client.get(f"/api/embed/{embed['public_id']}/bootstrap").status_code == 404
    # The shared agent survives.
    assert world.client.get(f"/api/data_sources/{ds['id']}", headers=_h(world.admin_token, world.org_id)).status_code == 200

"""E2E tests: skills always use 'intelligent' (smart) retrieval.

Covers the server-side enforcement that a `kind='skill'` instruction can never
have its `load_mode` set to 'always' or 'disabled' — on create, on update, and
when its kind is changed — plus that normal instructions are unaffected.
"""
import uuid

import pytest


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _new_admin(create_user, login_user, whoami):
    email = f"skill_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    me = whoami(token)
    return token, me["organizations"][0]["id"]


@pytest.mark.e2e
def test_create_skill_forces_intelligent(create_user, login_user, whoami, test_client):
    token, org_id = _new_admin(create_user, login_user, whoami)
    # Ask for 'always' but kind=skill — server must override to 'intelligent'.
    resp = test_client.post(
        "/api/instructions",
        json={
            "text": "When computing revenue, exclude refunds.",
            "title": "Revenue skill",
            "kind": "skill",
            "load_mode": "always",
            "status": "published",
            "category": "general",
        },
        headers=_auth(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["kind"] == "skill"
    assert body["load_mode"] == "intelligent", body


@pytest.mark.e2e
def test_create_normal_instruction_keeps_load_mode(create_user, login_user, whoami, test_client):
    token, org_id = _new_admin(create_user, login_user, whoami)
    resp = test_client.post(
        "/api/instructions",
        json={
            "text": "Always format currency with two decimals.",
            "kind": "instruction",
            "load_mode": "always",
            "status": "published",
        },
        headers=_auth(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["kind"] == "instruction"
    assert body["load_mode"] == "always", body


@pytest.mark.e2e
def test_update_to_skill_forces_intelligent(create_user, login_user, whoami, test_client):
    token, org_id = _new_admin(create_user, login_user, whoami)
    # Start as a normal 'always' instruction.
    created = test_client.post(
        "/api/instructions",
        json={
            "text": "Round percentages to one decimal.",
            "kind": "instruction",
            "load_mode": "always",
            "status": "published",
        },
        headers=_auth(token, org_id),
    ).json()
    assert created["load_mode"] == "always"

    # Flip kind -> skill. Even though load_mode isn't passed, it must become smart.
    upd = test_client.put(
        f"/api/instructions/{created['id']}",
        json={"kind": "skill"},
        headers=_auth(token, org_id),
    )
    assert upd.status_code == 200, upd.json()
    assert upd.json()["kind"] == "skill"
    assert upd.json()["load_mode"] == "intelligent", upd.json()


@pytest.mark.e2e
def test_bulk_update_load_mode_skipped_for_skill(create_user, login_user, whoami, test_client):
    token, org_id = _new_admin(create_user, login_user, whoami)
    skill = test_client.post(
        "/api/instructions",
        json={"text": "Skill body", "kind": "skill", "status": "published"},
        headers=_auth(token, org_id),
    ).json()
    assert skill["load_mode"] == "intelligent"

    # Bulk-set load_mode='always' across the skill — must stay 'intelligent'.
    resp = test_client.put(
        "/api/instructions/bulk",
        json={"ids": [skill["id"]], "load_mode": "always"},
        headers=_auth(token, org_id),
    )
    assert resp.status_code == 200, resp.json()

    got = test_client.get(
        f"/api/instructions/{skill['id']}", headers=_auth(token, org_id)
    ).json()
    assert got["load_mode"] == "intelligent", got

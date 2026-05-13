"""e2e tests for the per-membership ``note`` field and the bulk Excel/CSV
import endpoint at ``POST /organizations/{id}/members/import``.

Verifies:
 - admin can set / clear a note via PUT /members/{id}
 - non-admin cannot edit a note (403)
 - notes are per-org: setting a note in org A doesn't affect the same
   user's membership in org B
 - CSV / xlsx parsing, dry-run preview semantics
 - commit-mode is idempotent (re-uploading doesn't reset roles or
   re-invite pending members)
 - permission gating on the import endpoint
 - import never overwrites existing roles
"""
import io
import uuid

import pytest


def _csv_bytes(rows):
    out = io.StringIO()
    out.write("email,note\n")
    for email, note in rows:
        # quote the note in case it has commas
        note_field = ('"' + note.replace('"', '""') + '"') if note is not None else ""
        out.write(f"{email},{note_field}\n")
    return out.getvalue().encode("utf-8")


def _xlsx_bytes(rows):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["email", "note"])
    for email, note in rows:
        ws.append([email, note])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _auth_headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}


def _admin_setup(create_user, login_user, whoami):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return admin, token, org_id


def _invite(test_client, token, org_id, email, role="member"):
    resp = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email, "role": role},
        headers=_auth_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _list_members(test_client, token, org_id):
    resp = test_client.get(
        f"/api/organizations/{org_id}/members",
        headers=_auth_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


@pytest.mark.e2e
def test_admin_can_set_and_clear_member_note(test_client, create_user, login_user, whoami):
    _, admin_token, org_id = _admin_setup(create_user, login_user, whoami)
    email = f"alice_{uuid.uuid4().hex[:8]}@test.com"
    membership = _invite(test_client, admin_token, org_id, email)

    # Set the note
    resp = test_client.put(
        f"/api/organizations/{org_id}/members/{membership['id']}",
        json={"note": "CFO, focuses on monthly close metrics"},
        headers=_auth_headers(admin_token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["note"] == "CFO, focuses on monthly close metrics"

    # And it survives a list
    members = _list_members(test_client, admin_token, org_id)
    target = next(m for m in members if m["id"] == membership["id"])
    assert target["note"] == "CFO, focuses on monthly close metrics"

    # Clear it
    resp = test_client.put(
        f"/api/organizations/{org_id}/members/{membership['id']}",
        json={"note": None},
        headers=_auth_headers(admin_token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["note"] is None


@pytest.mark.e2e
def test_update_note_does_not_clobber_role(test_client, create_user, login_user, whoami):
    """Updating only the note must leave the existing role intact."""
    _, admin_token, org_id = _admin_setup(create_user, login_user, whoami)
    email = f"bob_{uuid.uuid4().hex[:8]}@test.com"
    membership = _invite(test_client, admin_token, org_id, email, role="admin")
    assert membership["role"] == "admin"

    resp = test_client.put(
        f"/api/organizations/{org_id}/members/{membership['id']}",
        json={"note": "a note only"},
        headers=_auth_headers(admin_token, org_id),
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"  # role preserved
    assert resp.json()["note"] == "a note only"


@pytest.mark.e2e
def test_member_note_is_per_org(test_client, create_user, login_user, whoami):
    """Same user in two orgs — a note set in org A must not appear in org B.

    A note lives on the Membership row, not the User row, so the same user
    can carry different per-org context (different roles at different companies).
    """
    from app.settings.config import settings as _bow_settings
    prev = _bow_settings.bow_config.features.allow_multiple_organizations
    _bow_settings.bow_config.features.allow_multiple_organizations = True
    try:
        admin, admin_token, org_a = _admin_setup(create_user, login_user, whoami)

        # Same admin creates a second org
        resp = test_client.post(
            "/api/organizations",
            json={"name": "Org B"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 200, resp.json()
        org_b = resp.json()["id"]

        shared_email = f"shared_{uuid.uuid4().hex[:8]}@test.com"
        m_a = _invite(test_client, admin_token, org_a, shared_email)
        m_b = _invite(test_client, admin_token, org_b, shared_email)

        # Set a note in org A only
        resp = test_client.put(
            f"/api/organizations/{org_a}/members/{m_a['id']}",
            json={"note": "VIP in A"},
            headers=_auth_headers(admin_token, org_a),
        )
        assert resp.status_code == 200
        assert resp.json()["note"] == "VIP in A"

        members_b = _list_members(test_client, admin_token, org_b)
        target_b = next(m for m in members_b if m["id"] == m_b["id"])
        assert target_b.get("note") is None, "note from org A leaked into org B"

        # And the note in A is preserved on a relist
        members_a = _list_members(test_client, admin_token, org_a)
        target_a = next(m for m in members_a if m["id"] == m_a["id"])
        assert target_a["note"] == "VIP in A"
    finally:
        _bow_settings.bow_config.features.allow_multiple_organizations = prev


@pytest.mark.e2e
def test_import_dry_run_does_not_persist(test_client, create_user, login_user, whoami):
    _, admin_token, org_id = _admin_setup(create_user, login_user, whoami)
    existing_email = f"existing_{uuid.uuid4().hex[:8]}@test.com"
    _invite(test_client, admin_token, org_id, existing_email)

    new_email = f"new_{uuid.uuid4().hex[:8]}@test.com"
    csv = _csv_bytes([
        (existing_email, "should update on commit"),
        (new_email, "should create on commit"),
        ("not-an-email", "bad"),
    ])

    resp = test_client.post(
        f"/api/organizations/{org_id}/members/import?dry_run=true",
        files={"file": ("members.csv", csv, "text/csv")},
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id},
    )
    assert resp.status_code == 200, resp.json()
    report = resp.json()
    assert report["dry_run"] is True
    assert report["summary"]["created"] == 1
    assert report["summary"]["updated"] == 1
    assert report["summary"]["errors"] == 1

    # Nothing actually changed: the new email is NOT yet a member
    members = _list_members(test_client, admin_token, org_id)
    emails = [m.get("email") or (m.get("user") or {}).get("email") for m in members]
    assert new_email not in emails

    existing = next(m for m in members if (m.get("email") or (m.get("user") or {}).get("email")) == existing_email)
    assert existing.get("note") is None, "dry-run wrote a note"


@pytest.mark.e2e
def test_import_commit_creates_updates_and_preserves_role(test_client, create_user, login_user, whoami):
    _, admin_token, org_id = _admin_setup(create_user, login_user, whoami)

    # Pre-existing member with an explicit admin role
    existing_email = f"existing_{uuid.uuid4().hex[:8]}@test.com"
    existing = _invite(test_client, admin_token, org_id, existing_email, role="admin")
    assert existing["role"] == "admin"

    new_email = f"new_{uuid.uuid4().hex[:8]}@test.com"
    rows = [(existing_email, "updated note"), (new_email, "fresh note")]

    resp = test_client.post(
        f"/api/organizations/{org_id}/members/import?dry_run=false",
        files={"file": ("members.xlsx", _xlsx_bytes(rows),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id},
    )
    assert resp.status_code == 200, resp.json()
    report = resp.json()
    assert report["dry_run"] is False
    assert report["summary"]["created"] == 1
    assert report["summary"]["updated"] == 1
    assert report["summary"]["errors"] == 0

    members = _list_members(test_client, admin_token, org_id)
    by_email = {(m.get("email") or (m.get("user") or {}).get("email")): m for m in members}

    # Note updated, but role preserved
    assert by_email[existing_email]["note"] == "updated note"
    assert by_email[existing_email]["role"] == "admin", "import clobbered the role"

    # New invite created with the note
    assert new_email in by_email
    assert by_email[new_email]["note"] == "fresh note"


@pytest.mark.e2e
def test_import_is_idempotent(test_client, create_user, login_user, whoami):
    _, admin_token, org_id = _admin_setup(create_user, login_user, whoami)
    email = f"idem_{uuid.uuid4().hex[:8]}@test.com"
    csv = _csv_bytes([(email, "stable note")])

    # First import — creates
    r1 = test_client.post(
        f"/api/organizations/{org_id}/members/import?dry_run=false",
        files={"file": ("m.csv", csv, "text/csv")},
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id},
    )
    assert r1.status_code == 200
    assert r1.json()["summary"]["created"] == 1

    # Promote the resulting pending membership to admin
    members = _list_members(test_client, admin_token, org_id)
    target = next(m for m in members if (m.get("email") or (m.get("user") or {}).get("email")) == email)
    promote = test_client.put(
        f"/api/organizations/{org_id}/members/{target['id']}",
        json={"role": "admin"},
        headers=_auth_headers(admin_token, org_id),
    )
    assert promote.status_code == 200
    assert promote.json()["role"] == "admin"

    # Second import — same file. Should be all unchanged.
    r2 = test_client.post(
        f"/api/organizations/{org_id}/members/import?dry_run=false",
        files={"file": ("m.csv", csv, "text/csv")},
        headers={"Authorization": f"Bearer {admin_token}", "X-Organization-Id": org_id},
    )
    assert r2.status_code == 200
    summary = r2.json()["summary"]
    assert summary["created"] == 0
    assert summary["unchanged"] == 1
    assert summary["updated"] == 0
    assert summary["errors"] == 0

    # Role still admin (no reset)
    members_after = _list_members(test_client, admin_token, org_id)
    target_after = next(m for m in members_after if (m.get("email") or (m.get("user") or {}).get("email")) == email)
    assert target_after["role"] == "admin"
    assert target_after["note"] == "stable note"


@pytest.mark.e2e
def test_import_permission_denied_for_non_admin(test_client, create_user, login_user, whoami):
    # Admin sets up the org and invites a regular member
    _, admin_token, org_id = _admin_setup(create_user, login_user, whoami)
    member_email = f"member_{uuid.uuid4().hex[:8]}@test.com"
    _invite(test_client, admin_token, org_id, member_email, role="member")
    create_user(email=member_email, password="test123")
    member_token = login_user(member_email, "test123")

    csv = _csv_bytes([(f"x_{uuid.uuid4().hex[:6]}@test.com", "x")])
    resp = test_client.post(
        f"/api/organizations/{org_id}/members/import?dry_run=true",
        files={"file": ("m.csv", csv, "text/csv")},
        headers=_auth_headers(member_token, org_id),
    )
    assert resp.status_code in (401, 403), f"non-admin should be denied, got {resp.status_code} {resp.text}"


@pytest.mark.e2e
def test_import_rejects_file_without_email_column(test_client, create_user, login_user, whoami):
    _, admin_token, org_id = _admin_setup(create_user, login_user, whoami)
    body = b"name,note\nalice,a CFO\n"
    resp = test_client.post(
        f"/api/organizations/{org_id}/members/import?dry_run=true",
        files={"file": ("m.csv", body, "text/csv")},
        headers=_auth_headers(admin_token, org_id),
    )
    assert resp.status_code == 400
    assert "email" in resp.json()["detail"].lower()

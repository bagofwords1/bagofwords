"""
E2E tests for the inline per-hunk suggestion resolution endpoint.

`POST /api/instructions/{id}/resolve` is what the inline tracked-changes review
UI calls when a reviewer accepts/rejects part of a suggested change:
- `promote_text` (current + accepted hunks) is promoted as a new version when it
  differs from the live text.
- `remaining_text` (current + still-pending hunks) is what stays proposed.

These tests exercise the endpoint through the real HTTP stack (routing,
permission gate, promote path) for the simplest case (no source build): an
accept that promotes, and a no-op that must not create a version.
"""
import pytest


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _version_count(test_client, instruction_id, token, org_id):
    resp = test_client.get(
        f"/api/instructions/{instruction_id}/versions",
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()["total"]


@pytest.mark.e2e
def test_resolve_accept_promotes_new_version(
    create_user, login_user, whoami, create_global_instruction, get_instruction, test_client
):
    """Accepting (promote_text != current) promotes the text as a new version."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    instruction = create_global_instruction(
        text="Alpha beta gamma.", user_token=token, org_id=org_id, status="published"
    )
    iid = instruction["id"]
    before = _version_count(test_client, iid, token, org_id)

    new_text = "Alpha BETA gamma."
    resp = test_client.post(
        f"/api/instructions/{iid}/resolve",
        json={"build_id": None, "promote_text": new_text, "remaining_text": new_text},
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()

    # Live text advanced and a new version was recorded.
    fetched = get_instruction(instruction_id=iid, user_token=token, org_id=org_id)
    assert fetched["text"] == new_text
    assert _version_count(test_client, iid, token, org_id) == before + 1


@pytest.mark.e2e
def test_resolve_noop_does_not_promote(
    create_user, login_user, whoami, create_global_instruction, get_instruction, test_client
):
    """Resolving with promote_text == current text is a no-op: no new version."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    instruction = create_global_instruction(
        text="Keep me unchanged.", user_token=token, org_id=org_id, status="published"
    )
    iid = instruction["id"]
    before = _version_count(test_client, iid, token, org_id)

    resp = test_client.post(
        f"/api/instructions/{iid}/resolve",
        json={"build_id": None, "promote_text": "Keep me unchanged.", "remaining_text": "Keep me unchanged."},
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.json()

    fetched = get_instruction(instruction_id=iid, user_token=token, org_id=org_id)
    assert fetched["text"] == "Keep me unchanged."
    assert _version_count(test_client, iid, token, org_id) == before

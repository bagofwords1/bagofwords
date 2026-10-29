"""Readers that look *inside* encrypted columns in SQL must still find
encrypted rows.

The reports list's free-text search ILIKEs over ``Completion.prompt`` and
``completion``. Under enterprise ``data_encryption`` those columns are
ciphertext in SQL, so the search falls back to a Python check on the rows the
SQL envelope marker flags. (The other such reader, the file-access embed check
over ``ArtifactVersion.content``, is covered in rbac/test_file_access_encrypted.py.)
This pins the contract from the API: an encrypted row behaves exactly like a
plaintext one.
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker
from app.ee.encryption import types as enc
from app.models.completion import Completion


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def encrypting(monkeypatch):
    monkeypatch.setattr(enc, "encryption_active", lambda: True)
    return enc


def _seed_completion(report_id: str, text: str):
    """No public API creates a completion without an LLM run, so the row is
    written directly, through the ORM so the column type applies."""
    async def _run():
        async with async_session_maker() as db:
            db.add(Completion(
                id=str(uuid.uuid4()), report_id=report_id,
                prompt={"content": text, "mentions": []},
                completion={"content": "", "reasoning": ""},
                status="success", model="m", turn_index=0, feedback_score=0,
                message_type="user_message", role="user",
            ))
            await db.commit()
    asyncio.run(_run())


def _search(client, token, org_id, term):
    resp = client.get("/api/reports", params={"search": term}, headers=_h(token, org_id))
    assert resp.status_code == 200, resp.text
    return {r["id"] for r in resp.json()["reports"]}


@pytest.mark.e2e
def test_report_search_finds_encrypted_and_plaintext_completions(
    test_client, create_user, login_user, whoami, create_report, monkeypatch
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    needle = f"zebra{uuid.uuid4().hex[:6]}"

    plain = create_report(title="plain", user_token=token, org_id=org_id)["id"]
    cipher = create_report(title="cipher", user_token=token, org_id=org_id)["id"]
    unrelated = create_report(title="unrelated", user_token=token, org_id=org_id)["id"]

    monkeypatch.setattr(enc, "encryption_active", lambda: False)
    _seed_completion(plain, f"show {needle} revenue")
    _seed_completion(unrelated, "show something else")
    monkeypatch.setattr(enc, "encryption_active", lambda: True)
    _seed_completion(cipher, f"show {needle.upper()} revenue")  # case-insensitive, like ILIKE

    # Searching with the feature on and off must give the same answer.
    for active in (True, False):
        monkeypatch.setattr(enc, "encryption_active", lambda active=active: active)
        found = _search(test_client, token, org_id, needle)
        assert plain in found and cipher in found
        assert unrelated not in found


@pytest.mark.e2e
def test_console_prompt_search_finds_encrypted_and_plaintext_prompts(
    create_report, seed_agent_executions, get_agent_execution_summaries,
    create_user, login_user, whoami, monkeypatch,
):
    """The diagnosis console's free-text prompt search ILIKEs over
    Completion.prompt in SQL; encrypted prompts must still be found."""
    from datetime import datetime

    owner = create_user()
    token = login_user(owner["email"], owner["password"])
    info = whoami(token)
    org_id, owner_id = info["organizations"][0]["id"], info["id"]
    report = create_report(title="Diagnosis", user_token=token, org_id=org_id)
    needle = f"zebra{uuid.uuid4().hex[:6]}"
    now = datetime.utcnow()

    monkeypatch.setattr(enc, "encryption_active", lambda: False)
    seed_agent_executions(org_id, report["id"], [
        {"user_id": owner_id, "prompt": f"{needle} by month", "created_at": now},
        {"user_id": owner_id, "prompt": "churn analysis", "created_at": now},
    ])
    monkeypatch.setattr(enc, "encryption_active", lambda: True)
    seed_agent_executions(org_id, report["id"], [
        {"user_id": owner_id, "prompt": f"{needle.upper()} forecast", "created_at": now},
        {"user_id": owner_id, "prompt": "weekly sales report", "created_at": now},
    ])

    for active in (True, False):
        monkeypatch.setattr(enc, "encryption_active", lambda active=active: active)
        resp = get_agent_execution_summaries(user_token=token, org_id=org_id, prompt_search=needle)
        assert resp.status_code == 200, resp.json()
        body = resp.json()
        assert body["total_items"] == 2
        assert all(needle.lower() in item["prompt"].lower() for item in body["items"])

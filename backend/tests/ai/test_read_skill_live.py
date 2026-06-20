"""Live AI test (Anthropic Haiku): the chat agent discovers a skill from the
<available_skills> catalog and pulls its full text via the read_skill tool.

Run:
  ANTHROPIC_API_KEY_TEST=sk-ant-... \
    pytest -s -m ai --db=sqlite tests/ai/test_read_skill_live.py

Skips cleanly when ANTHROPIC_API_KEY_TEST is unset.
"""
import os
import uuid
from pathlib import Path

import pytest

_SQLITE_DB = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _all_tool_names(completions):
    names = []
    for c in completions:
        for b in c.get("completion_blocks") or []:
            te = b.get("tool_execution") or {}
            if te.get("tool_name"):
                names.append(te["tool_name"])
    return names


@pytest.mark.ai
def test_agent_reads_skill_via_read_skill(
    create_user, login_user, whoami, test_client,
    create_data_source, create_report, create_completion,
):
    if not os.getenv("ANTHROPIC_API_KEY_TEST"):
        pytest.skip("ANTHROPIC_API_KEY_TEST is not set")

    # --- user / org ---
    email = f"liveskill_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    org_id = whoami(token)["organizations"][0]["id"]

    # --- Anthropic provider with ONLY Haiku so the planner runs on Haiku ---
    resp = test_client.post(
        "/api/llm/providers",
        json={
            "name": "anthropic haiku",
            "provider_type": "anthropic",
            "credentials": {"api_key": os.getenv("ANTHROPIC_API_KEY_TEST")},
            "models": [
                {"model_id": "claude-haiku-4-5-20251001", "name": "Claude 4.5 Haiku", "is_custom": False},
            ],
        },
        headers=_auth(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    default = [m for m in test_client.get("/api/llm/models", headers=_auth(token, org_id)).json() if m["is_default"]]
    assert default and "haiku" in default[0]["model_id"], default

    # --- data source (chinook sqlite) ---
    ds = create_data_source(
        name="chinook", type="sqlite", config={"database": str(_SQLITE_DB)},
        credentials={}, user_token=token, org_id=org_id,
    )

    # --- a published skill with a distinctive, self-identifying definition ---
    marker = "PURPLE-RHINO-42"
    skill = test_client.post(
        "/api/instructions",
        json={
            "text": (
                "Active-customer definition.\n\n"
                f"An 'active customer' is any customer with an invoice in the last 90 days. "
                f"The secret verification code for this definition is {marker}. "
                "Always apply this rule when the user asks about active customers."
            ),
            "title": "Active customer definition",
            "kind": "skill",
            "status": "published",
            "data_source_ids": [ds["id"]],
        },
        headers=_auth(token, org_id),
    )
    assert skill.status_code == 200, skill.json()
    assert skill.json()["load_mode"] == "intelligent"  # enforced

    report = create_report(
        title="Skill test", user_token=token, org_id=org_id, data_sources=[ds["id"]],
    )

    # --- drive the chat agent; prompt explicitly routes through the skill ---
    completions = create_completion(
        report_id=report["id"],
        prompt=(
            "There is a skill listed under available_skills about the active customer "
            "definition. Use the read_skill tool to read it, then tell me the secret "
            "verification code it contains. Do not query the database."
        ),
        user_token=token, org_id=org_id,
    )

    tool_names = _all_tool_names(completions)
    assert "read_skill" in tool_names, (
        f"agent did not call read_skill; tools used: {tool_names}"
    )

    # The agent should have surfaced the skill's content after reading it.
    blob = " ".join(
        (b.get("content") or "")
        for c in completions for b in (c.get("completion_blocks") or [])
    )
    read_outputs = " ".join(
        str((b.get("tool_execution") or {}).get("result_json") or "")
        for c in completions for b in (c.get("completion_blocks") or [])
    )
    assert marker in blob or marker in read_outputs, "skill content was not surfaced"

"""Live AI test (Anthropic Haiku): the chat agent finds entries deep inside a
LONG instruction — an event catalogue far larger than one read returns.

Before windowed reads, a catalogue this size reached the model cut at the
transcript's per-result cap, and anything past the cut was invisible: the agent
fell back to guessing or scanning the warehouse. The question below is only
answerable from entries placed well past the opening window, and it does not
name the tool or how to call it.

Run:
  ANTHROPIC_API_KEY_TEST=sk-ant-... \
    pytest -s -m ai --db=sqlite tests/ai/test_read_instruction_long_live.py

Skips cleanly when ANTHROPIC_API_KEY_TEST is unset.
"""
import os
import uuid
from pathlib import Path

import pytest

_SQLITE_DB = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()
_FILLER = ["connect", "battery", "sync", "firmware", "weight", "login", "goal"]


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _catalogue(hidden: list[str], *, n_events: int = 2400, hidden_at: int = 1900) -> str:
    lines = ["# Device app events (GL_APP_EVENTS_V)", "", "events:"]
    for i in range(n_events):
        if i == hidden_at:
            for name in hidden:
                lines += [
                    f"  {name}:",
                    "    description: Smart-scale Bluetooth pairing flow (scale pairing).",
                ]
        kw = _FILLER[i % len(_FILLER)]
        lines += [
            f"  app_{kw}_event_{i}:",
            f"    description: Fired when the user completes {kw} step {i} in the app.",
        ]
    return "\n".join(lines)


def _blocks(completions):
    return [b for c in completions for b in (c.get("completion_blocks") or [])]


@pytest.mark.ai
def test_agent_finds_entries_deep_in_a_long_instruction(
    create_user, login_user, whoami, test_client,
    create_data_source, create_report, create_completion,
):
    if not os.getenv("ANTHROPIC_API_KEY_TEST"):
        pytest.skip("ANTHROPIC_API_KEY_TEST is not set")

    email = f"longinstr_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    org_id = whoami(token)["organizations"][0]["id"]

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

    ds = create_data_source(
        name="device_app", type="sqlite", config={"database": str(_SQLITE_DB)},
        credentials={}, user_token=token, org_id=org_id,
    )

    suffix = uuid.uuid4().hex[:5]
    hidden = [
        f"scale_pair_started_{suffix}",
        f"scale_pair_handshake_ok_{suffix}",
        f"scale_pair_failed_{suffix}",
    ]
    body = _catalogue(hidden)
    instr = test_client.post(
        "/api/instructions",
        json={
            "text": body,
            "title": "Device app events catalogue",
            "description": "Every app event name in GL_APP_EVENTS_V with what it means.",
            "kind": "instruction",
            "load_mode": "intelligent",
            "status": "published",
            "data_source_ids": [ds["id"]],
        },
        headers=_auth(token, org_id),
    )
    assert instr.status_code == 200, instr.json()
    # Precondition: the hidden events sit past what one plain read returns.
    assert body.index(hidden[0]) > 100_000

    report = create_report(
        title="Long catalogue", user_token=token, org_id=org_id, data_sources=[ds["id"]],
    )
    completions = create_completion(
        report_id=report["id"],
        prompt=(
            "Which app events track smart-scale pairing? Use our device events catalogue "
            "and list the exact event names. Do not query the database."
        ),
        user_token=token, org_id=org_id,
    )

    reads = [
        (b.get("tool_execution") or {})
        for b in _blocks(completions)
        if (b.get("tool_execution") or {}).get("tool_name") == "read_instruction"
    ]
    assert reads, f"agent never read the catalogue; tools: {[ (b.get('tool_execution') or {}).get('tool_name') for b in _blocks(completions)]}"

    answer = " ".join((b.get("content") or "") for b in _blocks(completions))
    missing = [name for name in hidden if name not in answer]
    print("\nread_instruction calls:", [r.get("arguments_json") for r in reads])
    print("answer:", answer[-1500:])
    assert not missing, f"answer is missing events from deep in the catalogue: {missing}"

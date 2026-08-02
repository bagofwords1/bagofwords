"""Live AI test (Google Gemini): a basic question over the chinook music-store
database must run end to end through the completion service.

Regression guard for two Gemini-only breakages:

1. Tool-schema translation stripped any parameter whose *name* collided with a
   JSON Schema keyword, so `create_data.title` never reached Gemini and every
   call died with "Repeated validation failures: title: Field required".
2. A thinking_budget of 0 (the old default for every non-"pro" model) is
   rejected outright by current flash models — 400 INVALID_ARGUMENT before the
   request is even considered.

Run:
  GOOGLE_API_KEY_TEST=... \
    pytest -s -m ai --db=sqlite tests/ai/test_gemini_basic_question_live.py

Skips cleanly when GOOGLE_API_KEY_TEST is unset. GEMINI_TEST_MODEL overrides the
model under test (default: gemini-flash-latest).
"""
import os
import uuid
from pathlib import Path

import pytest

_SQLITE_DB = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()

_DEFAULT_MODEL = os.getenv("GEMINI_TEST_MODEL", "gemini-flash-latest")


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _tool_executions(completions):
    for c in completions:
        for b in c.get("completion_blocks") or []:
            te = b.get("tool_execution") or {}
            if te.get("tool_name"):
                yield te


def _skip_on_quota(completions):
    """Free-tier Gemini keys run out of requests-per-minute mid-run. That says
    nothing about the code under test, so don't fail the suite over it."""
    for c in completions:
        err = (c.get("completion") or {}).get("error") or {}
        if err.get("code") in ("quota", "rate_limit"):
            pytest.skip(f"Gemini quota/rate limit hit: {err.get('provider_message', '')[:200]}")


def _run_basic_question(model_id, create_user, login_user, whoami, test_client,
                        create_data_source, create_report, create_completion):
    email = f"gemini_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    org_id = whoami(token)["organizations"][0]["id"]

    resp = test_client.post(
        "/api/llm/providers",
        json={
            "name": f"google {model_id}",
            "provider_type": "google",
            "credentials": {"api_key": os.getenv("GOOGLE_API_KEY_TEST")},
            "models": [{"model_id": model_id, "name": model_id, "is_custom": True}],
        },
        headers=_auth(token, org_id),
    )
    assert resp.status_code == 200, resp.json()
    default = [m for m in test_client.get("/api/llm/models", headers=_auth(token, org_id)).json() if m["is_default"]]
    assert default and default[0]["model_id"] == model_id, default

    ds = create_data_source(
        name="chinook", type="sqlite", config={"database": str(_SQLITE_DB)},
        credentials={}, user_token=token, org_id=org_id,
    )
    report = create_report(
        title="Gemini basic question", user_token=token, org_id=org_id, data_sources=[ds["id"]],
    )

    completions = create_completion(
        report_id=report["id"],
        prompt="How many rows are in the invoices table?",
        user_token=token, org_id=org_id,
    )

    blob = " ".join(
        (b.get("content") or "")
        for c in completions for b in (c.get("completion_blocks") or [])
    )
    # Both bugs this test guards surface as a tool-validation failure, so check
    # that before the quota skip — a quota error must not mask a real regression.
    assert "Field required" not in blob, f"tool validation failed: {blob[:2000]}"
    _skip_on_quota(completions)

    executions = list(_tool_executions(completions))
    names = [te["tool_name"] for te in executions]
    assert "create_data" in names, f"agent never called create_data; tools used: {names}"

    for te in executions:
        if te["tool_name"] != "create_data":
            continue
        args = te.get("arguments_json") or {}
        if isinstance(args, dict) and args:
            assert args.get("title"), f"create_data called without a title: {args}"

    statuses = [te.get("status") for te in executions if te["tool_name"] == "create_data"]
    assert "success" in statuses, f"create_data never succeeded; statuses={statuses} blob={blob[:2000]}"

    return completions, blob


@pytest.mark.ai
def test_gemini_flash_answers_basic_question(
    create_user, login_user, whoami, test_client,
    create_data_source, create_report, create_completion,
):
    if not os.getenv("GOOGLE_API_KEY_TEST"):
        pytest.skip("GOOGLE_API_KEY_TEST is not set")

    _run_basic_question(
        _DEFAULT_MODEL, create_user, login_user, whoami, test_client,
        create_data_source, create_report, create_completion,
    )


@pytest.mark.ai
def test_gemini_pro_answers_basic_question(
    create_user, login_user, whoami, test_client,
    create_data_source, create_report, create_completion,
):
    if not os.getenv("GOOGLE_API_KEY_TEST"):
        pytest.skip("GOOGLE_API_KEY_TEST is not set")

    _run_basic_question(
        os.getenv("GEMINI_TEST_PRO_MODEL", "gemini-pro-latest"),
        create_user, login_user, whoami, test_client,
        create_data_source, create_report, create_completion,
    )

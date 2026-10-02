"""Paid qualification through the normal planner, without SDK hints in prompts.

Requires ARTIFACT_LIVE_EVAL_APPROVED=true and OPENAI_API_KEY_TEST. Keep this
matrix separate from authoring instructions. Do not repair prompts to make a
failed case pass. Generated apps still need browser interaction qualification.
"""

import os
import json
from pathlib import Path
import pytest

CASES = [
    ("notes", "Make a little app where I can jot down project notes and come back to them later.", {"collection"}),
    (
        "documents",
        "I want a small app: drop in a document, get an AI summary as it writes, and keep the summaries I like.",
        {"collection", "files", "ai"},
    ),
    ("blog", "Make me a simple blog site. I want to write posts, keep drafts, and publish when ready.", {"collection"}),
]


@pytest.mark.ai
@pytest.mark.parametrize("model_id", ["gpt-6-luna", "gpt-6-sol"])
@pytest.mark.parametrize("case,prompt,kinds", CASES, ids=[c[0] for c in CASES])
def test_ordinary_prompt_creates_resource_app(
    model_id,
    case,
    prompt,
    kinds,
    monkeypatch,
    tmp_path,
    create_user,
    login_user,
    whoami,
    test_client,
    create_report,
    create_completion,
):
    if os.environ.get("ARTIFACT_LIVE_EVAL_APPROVED") != "true" or not os.environ.get("OPENAI_API_KEY_TEST"):
        pytest.skip("Real-model authoring approval and test credential are required")
    monkeypatch.setenv("BOW_ARTIFACT_RESOURCES_ENABLED", "true")
    monkeypatch.setenv("BOW_ARTIFACT_STORAGE", str(tmp_path / "files"))
    user = create_user()
    token = login_user(user["email"], user["password"])
    org = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": "Bearer " + token, "X-Organization-Id": org}
    provider = test_client.post(
        "/api/llm/providers",
        headers=headers,
        json={
            "name": "Artifact qualification",
            "provider_type": "openai",
            "credentials": {"api_key": os.environ["OPENAI_API_KEY_TEST"]},
            "models": [{"model_id": model_id, "name": model_id, "is_custom": True}],
        },
    )
    assert provider.status_code == 200
    report = create_report(title="Artifact qualification", user_token=token, org_id=org, data_sources=[])
    completion = create_completion(report_id=report["id"], prompt=prompt, user_token=token, org_id=org)
    result = test_client.get(f"/api/artifacts/report/{report['id']}/latest", headers=headers)
    assert result.status_code == 200, "Planner did not produce an artifact"
    artifact = result.json()
    assert artifact["status"] == "completed"
    assert artifact["content"].get("sdk_version") == 1
    base = f"/api/artifacts/{artifact['artifact_id']}/runtime"
    response = test_client.get(base + "/resources", headers=headers)
    assert response.status_code == 200
    definitions = response.json()["items"]
    # Optional evidence contains synthetic output only, never tokens/credentials.
    if directory := os.environ.get("ARTIFACT_LIVE_EVIDENCE"):
        destination = Path(directory)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / f"{case}-{model_id}-{report['id']}.json").write_text(
            json.dumps(
                {
                    "model": model_id,
                    "case": case,
                    "prompt": prompt,
                    "completion": completion,
                    "artifact": artifact,
                    "resources": definitions,
                },
                indent=2,
            )
        )
    assert kinds <= {r["kind"] for r in definitions}
    if case == "documents":
        operation = next(r for r in definitions if r["kind"] == "ai")
        stream = test_client.post(
            base + "/ai/" + operation["name"] + "/stream",
            headers=headers,
            json={"text": "The meeting agreed to review invoices on Thursday. Ana owns the review."},
        )
        assert stream.status_code == 200
        events = [json.loads(line) for line in stream.text.splitlines()]
        assert any(e["type"] == "text_delta" for e in events)
        assert events[-1]["type"] == "completed" and events[-1]["output"].strip()
    if case == "blog":
        create_completion(
            report_id=report["id"],
            prompt="Only I should be able to change posts. Visitors can read published posts, but never drafts.",
            user_token=token,
            org_id=org,
        )
        current = test_client.get(base + "/resources", headers=headers).json()["items"]
        collections = [r for r in current if r["kind"] == "collection"]
        rules = [
            rule for r in collections for rule in (r["permissions"]["read"].get("any_of") or [r["permissions"]["read"]])
        ]
        assert any(r["audience"] == "public" and r.get("equals") for r in rules)
        assert all(
            rule["audience"] != "public"
            for r in collections
            for op in ("create", "update", "delete")
            for rule in (r["permissions"][op].get("any_of") or [r["permissions"][op]])
        )

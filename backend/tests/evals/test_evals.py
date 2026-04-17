"""Run every case in backend/evals/suites/*.yaml through the real agent
and assert the expectations pass.

Gated behind ``@pytest.mark.evals`` and an LLM key — see tests/evals/conftest.py.
"""

from pathlib import Path

import pytest

from tests.evals.conftest import ALL_EVAL_CASES


@pytest.mark.parametrize(
    "yaml_path,suite_name,case_name",
    ALL_EVAL_CASES,
    ids=[f"{s}/{c}" for _p, s, c in ALL_EVAL_CASES] or None,
)
def test_eval_case(
    yaml_path, suite_name, case_name,
    eval_env, import_suite_yaml, test_client, wait_for_run,
):
    token = eval_env["token"]
    org_id = eval_env["org_id"]

    yaml_text = Path(yaml_path).read_text()
    imported = import_suite_yaml(yaml_text, user_token=token, org_id=org_id)
    assert imported.status_code == 200, imported.json()
    case_id = imported.json()["cases_by_name"][case_name]

    # Kick off the agent via the batch endpoint (same path the UI uses).
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }
    resp = test_client.post(
        "/api/tests/runs/batch",
        json={"case_ids": [case_id], "trigger_reason": "eval"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()
    run = resp.json()

    results = wait_for_run(
        run["id"], user_token=token, org_id=org_id, timeout_s=240,
    )
    assert len(results) == 1
    result = results[0]
    assert result["status"] == "pass", {
        "case": f"{suite_name}/{case_name}",
        "status": result["status"],
        "failure_reason": result.get("failure_reason"),
        "result_json": result.get("result_json"),
    }

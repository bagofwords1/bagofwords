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
    eval_env, import_suite_yaml, run_case_and_wait,
):
    token = eval_env["token"]
    org_id = eval_env["org_id"]

    print(f"\n[eval] case={suite_name}/{case_name}", flush=True)

    yaml_text = Path(yaml_path).read_text()
    imported = import_suite_yaml(yaml_text, user_token=token, org_id=org_id)
    assert imported.status_code == 200, imported.json()
    case_id = imported.json()["cases_by_name"][case_name]
    print(f"[eval] imported case_id={case_id[:8]}", flush=True)

    results = run_case_and_wait(
        [case_id], user_token=token, org_id=org_id, timeout_s=300,
    )
    assert len(results) == 1
    result = results[0]
    assert result["status"] == "pass", {
        "case": f"{suite_name}/{case_name}",
        "status": result["status"],
        "failure_reason": result.get("failure_reason"),
        "result_json": result.get("result_json"),
    }

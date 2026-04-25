"""Pure-pydantic tests for the run_eval input validator.

The full ``run_eval`` tool requires a DB session, organization, agent
execution, etc. — it's exercised by the eval e2e harness. This file
covers the shape rules: case_ids and suite_id are mutually exclusive,
exactly one must be supplied.
"""
import pytest

from app.ai.tools.schemas.run_eval import RunEvalInput


def test_run_eval_input_accepts_case_ids():
    inp = RunEvalInput(case_ids=["a", "b"])
    assert inp.case_ids == ["a", "b"]
    assert inp.suite_id is None


def test_run_eval_input_accepts_suite_id():
    inp = RunEvalInput(suite_id="suite-uuid")
    assert inp.suite_id == "suite-uuid"
    assert inp.case_ids is None


def test_run_eval_input_rejects_both():
    with pytest.raises(Exception):
        RunEvalInput(case_ids=["a"], suite_id="suite-uuid")


def test_run_eval_input_rejects_neither():
    with pytest.raises(Exception):
        RunEvalInput()


def test_run_eval_input_rejects_empty_case_ids_without_suite():
    with pytest.raises(Exception):
        RunEvalInput(case_ids=[])

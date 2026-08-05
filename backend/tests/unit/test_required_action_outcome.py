"""Unit tests for the required-action outcome metric and typed create_data
preflight errors.

The metric exists because completion/agent-execution status only records that
the orchestration loop terminated normally: a run whose every create_data call
failed still ends status='success'. required_action_json records whether the
deliverable-producing action itself succeeded.
"""

from app.ai.agent_v2 import _compute_required_action_json


def _obs(tool_name, observation):
    return {"tool_name": tool_name, "tool_input": {}, "observation": observation}


class TestComputeRequiredActionJson:
    def test_no_tools_at_all(self):
        out = _compute_required_action_json([])
        assert out == {
            "required": False,
            "attempted": 0,
            "succeeded": 0,
            "success": None,
        }

    def test_none_input(self):
        out = _compute_required_action_json(None)
        assert out["required"] is False
        assert out["success"] is None

    def test_non_deliverable_tools_do_not_count(self):
        out = _compute_required_action_json(
            [_obs("inspect_data", {"summary": "ok"}), _obs("answer_question", {})]
        )
        assert out["required"] is False
        assert out["attempted"] == 0
        assert out["success"] is None

    def test_all_create_data_failed_is_not_success(self):
        # The exact production masking case: loop terminates normally, but no
        # create_data succeeded. The metric must say success=False.
        out = _compute_required_action_json(
            [
                _obs("create_data", {"summary": "no source", "error": {"type": "no_source_specified"}}),
                _obs("create_data", {"summary": "failed", "success": False}),
            ]
        )
        assert out == {
            "required": True,
            "attempted": 2,
            "succeeded": 0,
            "success": False,
        }

    def test_recovery_after_failure_is_success(self):
        out = _compute_required_action_json(
            [
                _obs("create_data", {"error": {"type": "no_source_specified"}}),
                _obs("create_data", {"summary": "created data", "success": True}),
            ]
        )
        assert out["required"] is True
        assert out["attempted"] == 2
        assert out["succeeded"] == 1
        assert out["success"] is True

    def test_success_flag_missing_treated_as_success(self):
        # Observations without success/error keys are non-failures, matching
        # _observation_failed's semantics.
        out = _compute_required_action_json([_obs("create_data", {"summary": "ok"})])
        assert out["success"] is True


class TestSchemaSourceGuidance:
    def test_schema_still_accepts_sourceless_call_but_describes_requirement(self):
        # The JSON-schema contract is intentionally unchanged (non-breaking);
        # prevention at this stage is prompt-level. Both source field
        # descriptions must state that every call needs a source.
        from app.ai.tools.schemas.create_data import CreateDataInput

        x = CreateDataInput(
            title="Revenue",
            user_prompt="show revenue",
            interpreted_prompt="calculate revenue",
        )
        assert x.tables_by_source is None and x.source_file_ids is None

        fields = CreateDataInput.model_fields
        tbs_desc = fields["tables_by_source"].description or ""
        sfi_desc = fields["source_file_ids"].description or ""
        assert "EVERY create_data call must name its data source" in tbs_desc
        assert "keep this empty" not in tbs_desc
        assert "at least one of the two" in sfi_desc

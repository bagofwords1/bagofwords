"""Contract tests for create_data's source-field guidance.

The largest active create_data failure class in production was planner calls
with no tables_by_source and no source_file_ids. The JSON-schema contract is
intentionally unchanged (non-breaking), so prevention at this stage is
prompt-level: both source field descriptions must tell the planner that every
call needs a source.
"""

from app.ai.tools.schemas.create_data import CreateDataInput


class TestSchemaSourceGuidance:
    def test_schema_still_accepts_sourceless_call(self):
        # Documents the current (deliberate) contract: a source-less call is
        # schema-valid and is rejected by the runtime preflight with a typed
        # `no_source_specified` error instead.
        x = CreateDataInput(
            title="Revenue",
            user_prompt="show revenue",
            interpreted_prompt="calculate revenue",
        )
        assert x.tables_by_source is None and x.source_file_ids is None

    def test_descriptions_demand_a_source(self):
        fields = CreateDataInput.model_fields
        tbs_desc = fields["tables_by_source"].description or ""
        sfi_desc = fields["source_file_ids"].description or ""
        assert "EVERY create_data call must name its data source" in tbs_desc
        assert "keep this empty" not in tbs_desc
        assert "at least one of the two" in sfi_desc

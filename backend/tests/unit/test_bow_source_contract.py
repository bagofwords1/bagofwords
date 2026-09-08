"""Public query contract: BOW is a typed source, never arbitrary internal SQL."""
import pytest
from pydantic import ValidationError

from app.schemas.bow_source_schema import BowQuery


@pytest.mark.parametrize("extra", [
    {"organization_id": "another-org"}, {"user_id": "another-user"},
    {"scope_ids": []}, {"sql": "select * from users"},
])
def test_source_queries_cannot_supply_authorization_or_sql(extra):
    with pytest.raises(ValidationError):
        BowQuery.model_validate({"dataset": "runs", **extra})


@pytest.mark.parametrize("dataset", ["runs", "tool_calls"])
def test_source_queries_have_bounded_defaults(dataset):
    query = BowQuery(dataset=dataset)
    assert query.time_range.relative == "30d"
    assert query.limit is None  # no silent top-N


def test_relative_and_fixed_time_ranges_are_mutually_exclusive():
    with pytest.raises(ValidationError):
        BowQuery(dataset="runs", time_range={"relative": "7d", "start": "2026-09-01T00:00:00Z", "end": "2026-09-02T00:00:00Z"})


@pytest.mark.asyncio
@pytest.mark.parametrize('swallow', [False, True])
async def test_infrastructure_failures_stop_codegen_retries_and_cannot_be_saved_as_success(swallow):
    from app.ai.code_execution.code_execution import StreamingCodeExecutor
    from app.ai.schemas.codegen import CodeGenContext, CodeGenRequest
    from app.data_sources.clients.bow_client import BowInfrastructureError

    class UnavailableDriver:
        def execute_query(self, request):
            raise BowInfrastructureError('The database is unavailable')

    async def codegen(**kwargs):
        if swallow:
            return '''def generate_df(ds_clients, excel_files):
    import pandas as pd
    try:
        return ds_clients["bow"].execute_query({"dataset":"runs"})
    except Exception:
        return pd.DataFrame({"count":[0]})
'''
        return 'def generate_df(ds_clients, excel_files):\n    return ds_clients["bow"].execute_query({"dataset":"runs"})'

    events = [event async for event in StreamingCodeExecutor().generate_and_execute_stream_v2(
        request=CodeGenRequest(context=CodeGenContext(user_prompt='Run counts', schemas_excerpt=''), retries=3),
        ds_clients={'bow':UnavailableDriver()}, excel_files=[], code_generator_fn=codegen)]
    assert not any(e['type'] == 'progress' and e['payload'].get('stage') == 'retry' for e in events)
    result = events[-1]['payload']
    assert result['df'] is None
    assert result['errors']
    assert result['query_timings'][-1]['terminal']

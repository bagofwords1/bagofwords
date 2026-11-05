"""A refused query fails its attempt, while the planner can finish other work."""
import sqlite3

import pytest

from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.code_execution.table_scope import TABLE_SCOPE_ATTR, TableScope
from app.ai.schemas.codegen import CodeGenContext, CodeGenRequest
from app.ai.tools.implementations.create_data import CreateDataTool
from app.data_sources.clients.bow_client import BowInfrastructureError
from app.data_sources.clients.sqlite_client import SqliteClient


async def execute(code, client):
    async def codegen(**kwargs):
        return code

    events = [event async for event in StreamingCodeExecutor().generate_and_execute_stream_v2(
        request=CodeGenRequest(context=CodeGenContext(user_prompt="Summarize sales", schemas_excerpt=""), retries=3),
        ds_clients={"sales": client}, excel_files=[], code_generator_fn=codegen,
    )]
    return events[-1]["payload"], events


@pytest.mark.asyncio
@pytest.mark.parametrize("swallow", [False, True])
@pytest.mark.parametrize("denied_table", ["private_orders", "restricted_customer_details"])
async def test_table_refusal_returns_control_to_planner_without_accepting_fallback(tmp_path, swallow, denied_table):
    path = tmp_path / "sales.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE orders (amount INTEGER)")
        db.execute("INSERT INTO orders VALUES (42)")
    client = SqliteClient(database=str(path))
    setattr(client, TABLE_SCOPE_ATTR, TableScope.from_names(["orders"], [denied_table]))
    query = f"SELECT * FROM {denied_table}"
    code = f'def generate_df(ds_clients, excel_files):\n    return ds_clients["sales"].execute_query({query!r})'
    if swallow:
        code = f'''def generate_df(ds_clients, excel_files):
    try:
        return ds_clients["sales"].execute_query({query!r})
    except Exception:
        return ds_clients["sales"].execute_query("SELECT * FROM orders")
'''
    result, events = await execute(code, client)
    assert result["df"] is None  # Even a nonempty fallback must not hide a refusal.
    assert result["errors"]
    assert not any(e["type"] == "progress" and e["payload"].get("stage") == "retry" for e in events)
    observation = CreateDataTool.build_execution_failure_observation(result["errors"], result["query_timings"])
    assert observation["error"]["type"] == "table_access_denied"
    assert not observation.get("analysis_complete")
    assert not observation.get("terminal_execution_error")
    assert not observation.get("final_answer")
    refused = next(t for t in result["query_timings"] if t.get("error"))
    assert observation["error"]["db_message"] == refused["error_message"]
    assert len(observation["error"]["db_message"]) > 200
    assert observation["error"]["failed_sql"] == query
    assert observation.get("retry_guidance")

    # A later, permitted query remains usable after the failed attempt.
    allowed, _ = await execute('def generate_df(ds_clients, excel_files):\n    return ds_clients["sales"].execute_query("SELECT * FROM orders")', client)
    assert allowed["df"]["amount"].tolist() == [42]


@pytest.mark.asyncio
@pytest.mark.parametrize("swallow", [False, True])
async def test_infrastructure_failure_still_ends_analysis(swallow):
    class UnavailableDriver:
        def execute_query(self, query):
            raise BowInfrastructureError("Database unavailable")

    code = 'def generate_df(ds_clients, excel_files):\n    return ds_clients["sales"].execute_query({"dataset": "runs"})'
    if swallow:
        code = '''def generate_df(ds_clients, excel_files):
    import pandas as pd
    try:
        return ds_clients["sales"].execute_query({"dataset": "runs"})
    except Exception:
        return pd.DataFrame({"count": [0]})
'''
    result, _ = await execute(code, UnavailableDriver())
    assert result["df"] is None
    observation = CreateDataTool.build_execution_failure_observation(result["errors"], result["query_timings"])
    assert observation["error"]["type"] == "infrastructure_failure"
    assert observation["analysis_complete"]
    assert observation["terminal_execution_error"]
    assert observation["final_answer"]


def test_later_nonterminal_error_does_not_hide_a_table_refusal():
    refusal = "A table is unavailable to this agent"
    observation = CreateDataTool.build_execution_failure_observation([], [
        {"error": refusal, "error_message": refusal, "error_type": "table_access_denied",
         "terminal": True, "sql": "SELECT * FROM private_data"},
        {"error": "Unknown column", "terminal": False, "sql": "SELECT typo FROM orders"},
    ])
    assert observation["error"]["type"] == "table_access_denied"
    assert observation["error"]["db_message"] == refusal
    assert not observation.get("analysis_complete")


def test_ordinary_query_failure_remains_correctable():
    observation = CreateDataTool.build_execution_failure_observation([], [
        {"error": "Unknown column", "terminal": False, "sql": "SELECT typo FROM orders"},
    ])
    assert observation["error"]["type"] == "execution_failure"
    assert not observation.get("analysis_complete")
    assert not observation.get("terminal_execution_error")
    assert not observation.get("final_answer")

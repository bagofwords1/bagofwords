"""Progress-heartbeat contract for long, quiet generated-code work.

Warehouse calls may be healthy while code generation or a synchronous driver
query emits nothing for several minutes.  The shared executor must keep the
tool stream active without changing the user-visible progress stage.
"""
from __future__ import annotations

import asyncio

import pandas as pd
import pytest

import app.ai.code_execution.code_execution as code_execution
from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.schemas.codegen import CodeGenContext, CodeGenRequest

_CODE = (
    "def generate_df(ds_clients, excel_files):\n"
    "    import pandas as pd\n"
    "    return pd.DataFrame({'value': [1]})\n"
)


def _request() -> CodeGenRequest:
    return CodeGenRequest(
        context=CodeGenContext(user_prompt="inspect", schemas_excerpt=""),
        retries=1,
    )


@pytest.mark.asyncio
async def test_quiet_codegen_emits_heartbeat_without_replacing_stage(monkeypatch):
    monkeypatch.setattr(
        code_execution, "_EXECUTION_HEARTBEAT_INTERVAL_S", 0.01, raising=False
    )
    release_codegen = asyncio.Event()

    async def quiet_codegen(**kwargs):
        await release_codegen.wait()
        return _CODE

    stream = StreamingCodeExecutor().generate_and_execute_stream_v2(
        request=_request(),
        ds_clients={},
        excel_files=[],
        code_generator_fn=quiet_codegen,
    )

    first = await stream.__anext__()
    heartbeat = await asyncio.wait_for(stream.__anext__(), timeout=0.2)

    assert first["payload"]["stage"] == "code_generation"
    assert heartbeat == {
        "type": "progress",
        "payload": {
            "stage": "code_generation",
            "attempt": 0,
            "heartbeat": True,
            "timing": False,
        },
    }

    release_codegen.set()
    await stream.aclose()


@pytest.mark.asyncio
async def test_quiet_query_emits_heartbeat_without_replacing_stage(monkeypatch):
    monkeypatch.setattr(
        code_execution, "_EXECUTION_HEARTBEAT_INTERVAL_S", 0.01, raising=False
    )
    release_query = asyncio.Event()
    executor = StreamingCodeExecutor()

    async def codegen(**kwargs):
        return _CODE

    async def quiet_execute(**kwargs):
        await release_query.wait()
        return pd.DataFrame({"value": [1]}), "", []

    monkeypatch.setattr(executor, "execute_code_async", quiet_execute)
    stream = executor.generate_and_execute_stream_v2(
        request=_request(),
        ds_clients={},
        excel_files=[],
        code_generator_fn=codegen,
    )

    while True:
        event = await stream.__anext__()
        if event.get("payload", {}).get("stage") == "data_query_execution":
            break
    heartbeat = await asyncio.wait_for(stream.__anext__(), timeout=0.2)

    assert heartbeat["payload"] == {
        "stage": "data_query_execution",
        "attempt": 0,
        "heartbeat": True,
        "timing": False,
    }

    release_query.set()
    await stream.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize('recovers', [True, False])
async def test_retry_history_does_not_turn_success_into_failure(recovers):
    attempts = []
    async def generate(**kwargs):
        attempts.append(1)
        if len(attempts) == 1 or not recovers:
            return "def generate_df(ds_clients, excel_files):\n    raise ValueError('synthetic transient failure')\n"
        return _CODE
    request = CodeGenRequest(context=CodeGenContext(user_prompt='inspect', schemas_excerpt=''), retries=2)
    events = [event async for event in StreamingCodeExecutor().generate_and_execute_stream_v2(
        request=request, ds_clients={}, excel_files=[], code_generator_fn=generate)]
    result = next(event['payload'] for event in events if event['type'] == 'done')
    if recovers:
        assert not result['df'].empty
        assert result['errors'] == []
        assert result['attempt_errors']
    else:
        assert result['df'] is None
        assert result['errors']


@pytest.mark.asyncio
async def test_a_query_waiting_for_a_connection_slot_is_reported_then_cleared(monkeypatch):
    # A query queued behind the connection's concurrency limit must say so
    # (and keep saying so on heartbeats), not look like a stuck "executing".
    monkeypatch.setattr(code_execution, "_EXECUTION_HEARTBEAT_INTERVAL_S", 0.05, raising=False)
    executor = StreamingCodeExecutor()

    async def codegen(**kwargs):
        return _CODE

    async def queued_execute(**kwargs):
        sink = kwargs["query_attribution"]["progress_sink"]
        assert kwargs["query_attribution"]["code_attempt"] == 1
        sink({"connection_name": "warehouse", "running": 3, "limit": 3, "queued_ahead": 1})
        await asyncio.sleep(0.8)
        sink(None)
        await asyncio.sleep(0.6)
        return pd.DataFrame({"value": [1]}), "", []

    monkeypatch.setattr(executor, "execute_code_async", queued_execute)
    events = [e async for e in executor.generate_and_execute_stream_v2(
        request=_request(), ds_clients={}, excel_files=[], code_generator_fn=codegen,
    )]
    stages = [e["payload"].get("stage") for e in events if e["type"] == "progress"]
    waiting = [e["payload"] for e in events if e["type"] == "progress"
               and e["payload"].get("stage") == "waiting_for_slot"]

    assert waiting and waiting[0]["running"] == 3 and waiting[0]["limit"] == 3
    assert waiting[0]["connection_name"] == "warehouse"
    first_wait = stages.index("waiting_for_slot")
    # Every heartbeat while waiting repeats the wait, never "executing".
    resumed = stages.index("data_query_execution", first_wait)
    assert all(s == "waiting_for_slot" for s in stages[first_wait:resumed])
    assert events[-1]["type"] == "done" and not events[-1]["payload"]["df"].empty

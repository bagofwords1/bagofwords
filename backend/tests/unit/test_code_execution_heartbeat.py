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

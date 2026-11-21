"""Stop (sigkill) must abort the generated-code work that is in flight.

The user's Stop only sets the run's ``sigkill_event``. The shared executor
behind create_data / inspect_data / write_csv used to check that flag only
*between* stages, so a long warehouse query kept running — and kept the
sandbox child and a code-exec thread busy — until it finished on its own.

Contract pinned here:
- Stop during query execution cancels the running statement at the source
  and ends the stream promptly with a ``stopped`` result.
- Stop during code generation cancels the codegen call.
- Without Stop, nothing changes: the result arrives as before.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pandas as pd
import pytest

from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.schemas.codegen import CodeGenContext, CodeGenRequest

# Long enough that "finished on its own" can never be mistaken for "stopped".
_QUERY_SECONDS = 30.0


def _request() -> CodeGenRequest:
    return CodeGenRequest(
        context=CodeGenContext(user_prompt="slow query", schemas_excerpt=""),
        retries=1,
    )


def _code(client_key: str) -> str:
    return (
        "def generate_df(ds_clients, excel_files):\n"
        f"    return ds_clients[{client_key!r}].execute_query('SELECT pg_sleep(30)')\n"
    )


class _SlowWarehouse:
    """A source whose query runs until the database is told to cancel it."""

    def __init__(self):
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def execute_query(self, query):
        self.started.set()
        if self.cancelled.wait(timeout=_QUERY_SECONDS):
            raise RuntimeError("canceling statement due to user request")
        return pd.DataFrame({"done": [1]})


async def _drain_until_done(stream):
    async for event in stream:
        if event["type"] == "done":
            return event["payload"]
    raise AssertionError("stream ended without a done event")


@pytest.mark.asyncio
@pytest.mark.parametrize("client_key", ["warehouse", "pg_orders"])
async def test_stop_during_query_cancels_it_at_the_source(monkeypatch, client_key):
    from app.data_sources import query_cancellation

    source = _SlowWarehouse()

    def cancel_thread(client, thread_ident):
        # The driver boundary: this is where a real client issues
        # pg_cancel_backend / KILL QUERY on a side connection.
        assert client is source and thread_ident
        source.cancelled.set()
        return "cancelled"

    monkeypatch.setattr(query_cancellation, "cancel_thread", cancel_thread)

    async def codegen(**kwargs):
        return _code(client_key)

    sigkill = asyncio.Event()
    stream = StreamingCodeExecutor().generate_and_execute_stream_v2(
        request=_request(),
        ds_clients={client_key: source},
        excel_files=[],
        code_generator_fn=codegen,
        sigkill_event=sigkill,
    )
    done_task = asyncio.ensure_future(_drain_until_done(stream))

    started = await asyncio.get_running_loop().run_in_executor(None, source.started.wait, 30)
    assert started, "the query never reached the source"

    t0 = time.monotonic()
    sigkill.set()
    payload = await asyncio.wait_for(done_task, timeout=15)
    elapsed = time.monotonic() - t0

    assert payload.get("stopped") is True
    assert elapsed < _QUERY_SECONDS / 2
    # The source-side cancel is issued by the sandbox runner's abort thread,
    # which may land just after the stream has already reported the stop.
    cancelled = await asyncio.get_running_loop().run_in_executor(None, source.cancelled.wait, 10)
    assert cancelled, "Stop did not ask the source to cancel the query"


@pytest.mark.asyncio
async def test_stop_during_codegen_cancels_the_codegen_call():
    codegen_cancelled = asyncio.Event()
    codegen_started = asyncio.Event()

    async def slow_codegen(**kwargs):
        codegen_started.set()
        try:
            await asyncio.sleep(_QUERY_SECONDS)
        except asyncio.CancelledError:
            codegen_cancelled.set()
            raise
        return _code("warehouse")

    sigkill = asyncio.Event()
    stream = StreamingCodeExecutor().generate_and_execute_stream_v2(
        request=_request(),
        ds_clients={},
        excel_files=[],
        code_generator_fn=slow_codegen,
        sigkill_event=sigkill,
    )
    done_task = asyncio.ensure_future(_drain_until_done(stream))
    await asyncio.wait_for(codegen_started.wait(), timeout=5)

    sigkill.set()
    payload = await asyncio.wait_for(done_task, timeout=5)

    assert payload.get("stopped") is True
    assert codegen_cancelled.is_set()


@pytest.mark.asyncio
async def test_unset_stop_event_leaves_results_untouched():
    async def codegen(**kwargs):
        return (
            "def generate_df(ds_clients, excel_files):\n"
            "    import pandas as pd\n"
            "    return pd.DataFrame({'value': [1, 2]})\n"
        )

    payload = await _drain_until_done(
        StreamingCodeExecutor().generate_and_execute_stream_v2(
            request=_request(),
            ds_clients={},
            excel_files=[],
            code_generator_fn=codegen,
            sigkill_event=asyncio.Event(),
        )
    )

    assert not payload.get("stopped")
    assert payload["df"]["value"].tolist() == [1, 2]
